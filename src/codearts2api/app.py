"""FastAPI application: OpenAI-compatible gateway, account pool, and WebUI."""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from codearts2api.account_files import account_filename
from fastapi.staticfiles import StaticFiles

from codearts2api import __version__
from codearts2api.auth import get_settings, require_api_key, require_upstream_credentials
from codearts2api.chat import (
    format_sse_done,
    format_sse_json,
    normalize_chunk,
    normalize_completion,
    openai_error,
)
from codearts2api.client import SnapAccessClient, UpstreamError
from codearts2api.config import Settings
from codearts2api.credentials import CredentialResolver
from codearts2api.logging_setup import configure_logging
from codearts2api.models_catalog import filter_and_map_models, strip_codearts_prefix
from codearts2api.oauth.dpop import generate_p256_keypair
from codearts2api.oauth.flow import CallbackServer, LoginError, build_authorize_url
from codearts2api.oauth.pkce import generate_pkce_pair, generate_ticket_id
from codearts2api.operations import (
    list_models_with_failover,
    open_chat_stream_with_failover,
    post_chat_with_failover,
    probe_account,
    run_benefit_claim_for_accounts,
    run_checkin_for_accounts,
)
from codearts2api.pool import AccountPool, collect_redaction_secrets
from codearts2api.scheduler import CheckinScheduler
from codearts2api.schemas import AccountCreateRequest, AccountUpdateRequest, ChatCompletionsRequest, CheckinRequest

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ModelCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = ttl_seconds
        self._expires_at = 0.0
        self._items: list[dict[str, Any]] | None = None

    def get(self) -> list[dict[str, Any]] | None:
        if self._items is None or time.monotonic() >= self._expires_at:
            return None
        return self._items

    def set(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self._expires_at = time.monotonic() + self.ttl

    def clear(self) -> None:
        self._items = None
        self._expires_at = 0.0


class AppState:
    def __init__(self, settings: Settings, pool: AccountPool) -> None:
        self.settings = settings
        self.pool = pool
        self.http: httpx.AsyncClient | None = None
        self.client: SnapAccessClient | None = None
        self.resolver = CredentialResolver(settings, store=pool.session_store, pool=pool)
        self.scheduler: CheckinScheduler | None = None
        self.model_cache = ModelCache(settings.model_cache_ttl)
        self.last_checkin: dict[str, Any] | None = None
        self.last_checkin_at: str | None = None
        self.last_checkin_accounts: list[dict[str, Any]] | None = None
        self.login_server: CallbackServer | None = None
        self.login_authorize_url: str | None = None
        self.login_account_id: str | None = None

    def snap(self) -> SnapAccessClient:
        if self.client is None:
            raise HTTPException(status_code=503, detail="HTTP client is not ready")
        return self.client


def _state(request: Request) -> AppState:
    return request.app.state.runtime


def refresh_logging(settings: Settings, pool: AccountPool) -> None:
    configure_logging(settings.log_level, collect_redaction_secrets(settings, pool))


def apply_runtime_side_effects(
    runtime: AppState,
    *,
    previous_cron: str | None = None,
    previous_timezone: str | None = None,
    previous_scheduler_enabled: bool | None = None,
) -> None:
    settings = runtime.settings
    refresh_logging(settings, runtime.pool)
    runtime.model_cache.ttl = settings.model_cache_ttl
    if runtime.http is not None:
        runtime.http.timeout = httpx.Timeout(settings.http_timeout)

    want_enabled = settings.scheduler_enabled
    if runtime.scheduler is None:
        if want_enabled:
            runtime.scheduler = CheckinScheduler(
                cron=settings.checkin_cron,
                timezone=settings.timezone,
                job=lambda: _execute_checkin(runtime),
            )
            runtime.scheduler.start()
        return

    runtime.scheduler.set_job(lambda: _execute_checkin(runtime))
    cron_changed = previous_cron is not None and previous_cron != settings.checkin_cron
    tz_changed = previous_timezone is not None and previous_timezone != settings.timezone
    enabled_changed = previous_scheduler_enabled is not None and previous_scheduler_enabled != want_enabled
    if enabled_changed:
        runtime.scheduler.set_enabled(want_enabled)
    if want_enabled and (cron_changed or tz_changed or not enabled_changed):
        runtime.scheduler.reschedule(cron=settings.checkin_cron, timezone=settings.timezone)


async def _execute_checkin(
    runtime: AppState,
    all_claimable: bool | None = None,
    account_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    claim_all = runtime.settings.checkin_claim_all if all_claimable is None else all_claimable
    if account_id:
        account = runtime.pool.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
        accounts = [account]
    else:
        accounts = runtime.pool.enabled_accounts()
        if not accounts:
            raise HTTPException(status_code=503, detail="No enabled accounts in the pool")
    per_account, overall, checked_in_at = await run_checkin_for_accounts(
        client=runtime.snap(),
        resolver=runtime.resolver,
        pool=runtime.pool,
        accounts=accounts,
        campaign_type=runtime.settings.campaign_type,
        all_claimable=claim_all,
        timezone_name=runtime.settings.timezone,
    )
    runtime.last_checkin = overall
    runtime.last_checkin_at = checked_in_at
    runtime.last_checkin_accounts = per_account
    logger.info(
        "check-in finished status=%s accounts=%s",
        overall.get("status"),
        len(per_account),
    )
    return per_account, overall, checked_in_at


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    pool = AccountPool(settings)
    pool.load_or_migrate()
    refresh_logging(settings, pool)
    runtime = AppState(settings, pool)
    # Per-app router: a module-level router accumulates routes on every factory call,
    # which produces duplicate OpenAPI operations once more than one app is built.
    protected = APIRouter(dependencies=[Depends(require_api_key)])
    static_dir = Path(STATIC_DIR)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(settings.http_timeout)
        runtime.http = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        runtime.client = SnapAccessClient(settings, runtime.http, runtime.resolver)
        app.state.runtime = runtime
        app.state.settings = settings

        if settings.scheduler_enabled:
            runtime.scheduler = CheckinScheduler(
                cron=settings.checkin_cron,
                timezone=settings.timezone,
                job=lambda: _execute_checkin(runtime),
            )
            runtime.scheduler.start()
            if settings.checkin_run_on_startup and runtime.resolver.has_any():
                try:
                    await _execute_checkin(runtime)
                except Exception:  # noqa: BLE001
                    logger.exception("Startup check-in failed; scheduler will retry on schedule")
            elif settings.checkin_run_on_startup:
                logger.warning(
                    "Startup check-in skipped: no enabled accounts in the pool. "
                    "Add an account in the WebUI, run `uv run codearts2api login`, or set IAM keys."
                )

        try:
            yield
        finally:
            if runtime.login_server is not None:
                runtime.login_server.stop()
                runtime.login_server = None
            if runtime.scheduler is not None:
                runtime.scheduler.shutdown()
            if runtime.http is not None:
                await runtime.http.aclose()

    app = FastAPI(
        title="codearts2api",
        version=__version__,
        description="Huawei Cloud China 码道 CodeArts Agent to OpenAI-compatible API gateway",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runtime = runtime

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, Any]:
        current = request.app.state.runtime
        ready = current.resolver.has_any() and settings.has_api_key()
        pool_status = current.pool.status_summary()
        return {
            "status": "ok" if ready else "degraded",
            "credentials_configured": current.resolver.has_any(),
            "api_key_configured": settings.has_api_key(),
            "credential_status": {
                "credential_source": settings.credential_source,
                "env_configured": current.resolver.env_credential() is not None,
                "pool_count": pool_status["count"],
                "pool_enabled": pool_status["enabled"],
                "strategy": pool_status["strategy"],
            },
        }

    def _ui_index() -> FileResponse:
        index = static_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="WebUI is not packaged")
        return FileResponse(index, media_type="text/html; charset=utf-8")

    # Exact routes only: API, health, docs and /static keep their own handlers.
    # HEAD is accepted so static hosts and health probes that send HEAD get the
    # same response class instead of a 405.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def ui_root() -> FileResponse:
        return _ui_index()

    @app.api_route("/ui", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/ui/", methods=["GET", "HEAD"], include_in_schema=False)
    async def ui_page(request: Request) -> RedirectResponse:
        # Local-path Location: this app's own route plus the mount/root_path in scope.
        # `request.url_for` resolves through the outer router and can pick a sibling
        # mount when the same app is mounted twice.
        prefix = request.scope.get("root_path", "").rstrip("/")
        return RedirectResponse(url=f"{prefix}{app.url_path_for('ui_root')}", status_code=307)

    @protected.get("/v1/models")
    async def list_models(
        request: Request,
        refresh: bool = Query(default=False),
        current_settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        runtime = _state(request)
        if not runtime.resolver.has_any():
            require_upstream_credentials(current_settings)
        if not refresh:
            cached = runtime.model_cache.get()
            if cached is not None:
                return {"object": "list", "data": cached, "cached": True}
        try:
            payload = await list_models_with_failover(
                client=runtime.snap(),
                resolver=runtime.resolver,
                pool=runtime.pool,
            )
        except UpstreamError as exc:
            raise HTTPException(status_code=exc.status_code, detail=openai_error(str(exc), exc.status_code)["error"]) from exc
        items = filter_and_map_models(payload, created=int(time.time()))
        runtime.model_cache.set(items)
        return {"object": "list", "data": items, "cached": False}

    @protected.post("/v1/chat/completions")
    async def chat_completions(
        body: ChatCompletionsRequest,
        request: Request,
        current_settings: Settings = Depends(get_settings),
    ):
        runtime = _state(request)
        if not runtime.resolver.has_any():
            require_upstream_credentials(current_settings)
        model = strip_codearts_prefix(body.model)
        upstream_payload = body.model_dump(exclude_none=True)
        upstream_payload["model"] = model

        if body.stream:
            return StreamingResponse(
                _stream_chat(runtime, upstream_payload, model),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        try:
            raw = await post_chat_with_failover(
                client=runtime.snap(),
                resolver=runtime.resolver,
                pool=runtime.pool,
                payload=upstream_payload,
            )
        except UpstreamError as exc:
            return JSONResponse(
                status_code=exc.status_code if exc.status_code >= 400 else 502,
                content=openai_error(str(exc), exc.status_code),
            )
        return normalize_completion(raw, model)

    @protected.post("/admin/checkin")
    async def post_checkin(
        request: Request,
        body: CheckinRequest | None = None,
        current_settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        runtime = _state(request)
        if not runtime.resolver.has_any():
            require_upstream_credentials(current_settings)
        all_claimable = body.all_claimable if body is not None else False
        account_id = body.account_id if body is not None else None
        per_account, overall, checked_in_at = await _execute_checkin(
            runtime,
            all_claimable=all_claimable,
            account_id=account_id,
        )
        return {
            "result": overall,
            "checked_in_at": checked_in_at,
            "accounts": per_account,
        }

    @protected.get("/admin/checkin")
    @protected.get("/admin/status")
    async def get_checkin_status(request: Request) -> dict[str, Any]:
        runtime = _state(request)
        scheduler = runtime.scheduler.snapshot() if runtime.scheduler else {
            "enabled": False,
            "cron": runtime.settings.checkin_cron,
            "timezone": runtime.settings.timezone,
            "last_run_at": None,
            "next_run_at": None,
            "last_error": None,
        }
        account_type = None
        is_credit = None
        if runtime.last_checkin:
            account_type = runtime.last_checkin.get("account_type")
            is_credit = runtime.last_checkin.get("is_credit_package")
        return {
            "last_checkin_at": runtime.last_checkin_at,
            "last_checkin": runtime.last_checkin,
            "accounts": runtime.last_checkin_accounts,
            "account_type": account_type,
            "is_credit_package": is_credit,
            "scheduler": scheduler,
            "campaign_type": runtime.settings.campaign_type,
            "claim_all_default": runtime.settings.checkin_claim_all,
            "credentials": runtime.resolver.status(),
            "pool": runtime.pool.status_summary(),
        }

    def _start_login(runtime: AppState, account_id: str | None, callback_port: int) -> dict[str, Any]:
        if account_id:
            account = runtime.pool.get(account_id)
            if account is None:
                raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
            if account.kind != "oauth":
                raise HTTPException(status_code=400, detail="OAuth login can only be started for oauth accounts")
        if runtime.login_server is not None:
            return {
                "authorize_url": runtime.login_authorize_url,
                "callback_url": runtime.login_server.callback_url,
                "status": "pending",
                "account_id": runtime.login_account_id,
                "message": "已有登录正在等待本机回调，请在浏览器中打开授权地址。",
            }
        endpoints = runtime.resolver.endpoints()
        pkce = generate_pkce_pair()
        ticket_id = generate_ticket_id()
        dpop = generate_p256_keypair()
        server = CallbackServer(endpoints, port=callback_port)
        server.start(pkce, ticket_id, dpop)
        authorize_url = build_authorize_url(
            endpoints,
            port=server.port,
            code_challenge=pkce.code_challenge,
            ticket_id=ticket_id,
            code_challenge_method=pkce.code_challenge_method,
        )
        runtime.login_server = server
        runtime.login_authorize_url = authorize_url
        runtime.login_account_id = account_id
        return {
            "authorize_url": authorize_url,
            "callback_url": server.callback_url,
            "status": "pending",
            "account_id": account_id,
            "client_id": endpoints.client_id,
            "message": (
                "在浏览器中打开 authorize_url，登录华为云并确认授权。"
                "官方客户端回调地址为 http://127.0.0.1:<port>/oauth/callback。"
                "若浏览器与本进程不在同一台机器，请做本机端口转发，"
                "或在有浏览器的机器上运行 `uv run codearts2api login`。"
                "然后调用 GET /admin/login/status。"
            ),
        }

    def _login_status_payload(runtime: AppState) -> dict[str, Any]:
        server = runtime.login_server
        if server is None:
            return {
                "status": "idle",
                "account_id": None,
                "session": runtime.resolver.status(),
            }
        if server._done.is_set():  # noqa: SLF001 — poll the in-process login
            account_id = runtime.login_account_id
            try:
                session = server.wait(timeout=0.1)
                if account_id:
                    account = runtime.pool.get(account_id)
                    if account is None:
                        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
                    runtime.pool.attach_session(account, session)
                else:
                    account = runtime.pool.find_or_create_oauth(use_legacy_session=True)
                    runtime.pool.attach_session(account, session)
                    account_id = account.id
                refresh_logging(runtime.settings, runtime.pool)
            except LoginError as exc:
                return {"status": "error", "message": str(exc), "account_id": account_id}
            finally:
                server.stop()
                runtime.login_server = None
                runtime.login_authorize_url = None
                runtime.login_account_id = None
            return {
                "status": "logged_in",
                "account_id": account_id,
                "session": runtime.resolver.status(),
            }
        return {
            "status": "pending",
            "account_id": runtime.login_account_id,
            "authorize_url": runtime.login_authorize_url,
            "callback_url": server.callback_url,
        }

    @protected.get("/admin/login")
    async def get_admin_login(
        request: Request,
        account_id: str | None = Query(default=None),
        current_settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        """Start (or resume) the official CLI loopback OAuth flow and return the authorize URL."""
        return _start_login(_state(request), account_id, current_settings.oauth_callback_port)

    @protected.get("/admin/login/status")
    async def get_admin_login_status(request: Request) -> dict[str, Any]:
        return _login_status_payload(_state(request))

    @protected.post("/admin/benefit-claim")
    async def post_benefit_claim(
        request: Request,
        body: CheckinRequest | None = None,
        current_settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        """Optional, separate from daily check-in. Requires OPENGW_BASE_URL."""
        runtime = _state(request)
        if not runtime.resolver.has_any():
            require_upstream_credentials(current_settings)
        account_id = body.account_id if body is not None else None
        if account_id:
            account = runtime.pool.get(account_id)
            if account is None:
                raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
            accounts = [account]
        else:
            accounts = runtime.pool.enabled_accounts()
            if not accounts:
                raise HTTPException(status_code=503, detail="No enabled accounts in the pool")
        per_account, overall, claimed_at = await run_benefit_claim_for_accounts(
            client=runtime.snap(),
            resolver=runtime.resolver,
            pool=runtime.pool,
            accounts=accounts,
            timezone_name=runtime.settings.timezone,
        )
        payload = dict(overall)
        payload["claimed_at"] = claimed_at
        payload["accounts"] = per_account
        return payload

    @protected.get("/admin/config")
    async def get_admin_config(request: Request) -> dict[str, Any]:
        return _state(request).pool.public_config()

    @protected.put("/admin/config")
    async def put_admin_config(request: Request) -> dict[str, Any]:
        runtime = _state(request)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")
        previous_cron = runtime.settings.checkin_cron
        previous_timezone = runtime.settings.timezone
        previous_scheduler_enabled = runtime.settings.scheduler_enabled
        try:
            payload = runtime.pool.update_runtime_settings(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        apply_runtime_side_effects(
            runtime,
            previous_cron=previous_cron,
            previous_timezone=previous_timezone,
            previous_scheduler_enabled=previous_scheduler_enabled,
        )
        return payload

    @protected.get("/admin/accounts")
    async def get_admin_accounts(request: Request) -> dict[str, Any]:
        runtime = _state(request)
        return {"accounts": runtime.pool.public_accounts()}

    @protected.post("/admin/accounts")
    async def post_admin_accounts(body: AccountCreateRequest, request: Request) -> dict[str, Any]:
        runtime = _state(request)
        try:
            account = runtime.pool.add_account(
                name=body.name,
                kind=body.kind,
                enabled=body.enabled,
                note=body.note,
                ak=body.ak,
                sk=body.sk,
                security_token=body.security_token,
                project_id=body.project_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        refresh_logging(runtime.settings, runtime.pool)
        return runtime.pool.public_account(account)

    @protected.post("/admin/accounts/import")
    async def import_admin_accounts(request: Request) -> dict[str, Any]:
        # Bound request size before parsing to avoid unbounded in-memory uploads.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Auth import exceeds 4 MiB")
        try:
            payload = json.loads(raw)
            files = payload.get("files") if isinstance(payload, dict) else None
            if not isinstance(files, list):
                raise ValueError("Expected {files: [auth-file, ...]}")
            runtime = _state(request)
            accounts = runtime.pool.import_accounts(files)
        except (ValueError, UnicodeError) as exc:
            # Parser/validation errors must never echo uploaded credentials.
            detail = "Invalid auth JSON" if isinstance(exc, (json.JSONDecodeError, UnicodeError)) else str(exc)
            raise HTTPException(status_code=400, detail=detail) from None
        refresh_logging(runtime.settings, runtime.pool)
        return {"accounts": [runtime.pool.public_account(a) for a in accounts]}

    @protected.get("/admin/accounts/{account_id}/export")
    async def export_admin_account(account_id: str, request: Request) -> JSONResponse:
        try:
            payload = _state(request).pool.export_account(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Account not found") from None
        return JSONResponse(payload, headers={
            "Content-Disposition": f'attachment; filename="{account_filename(account_id)}"',
            "Cache-Control": "no-store", "Pragma": "no-cache",
        })

    @protected.patch("/admin/accounts/{account_id}")
    async def patch_admin_account(
        account_id: str,
        body: AccountUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        runtime = _state(request)
        try:
            account = runtime.pool.update_account(account_id, body.model_dump(exclude_unset=True))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Account not found: {account_id}") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        refresh_logging(runtime.settings, runtime.pool)
        return runtime.pool.public_account(account)

    @protected.delete("/admin/accounts/{account_id}")
    async def delete_admin_account(account_id: str, request: Request) -> dict[str, Any]:
        runtime = _state(request)
        try:
            runtime.pool.delete_account(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Account not found: {account_id}") from None
        return {"ok": True, "id": account_id}

    @protected.post("/admin/accounts/{account_id}/test")
    async def post_admin_account_test(account_id: str, request: Request) -> dict[str, Any]:
        runtime = _state(request)
        account = runtime.pool.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
        try:
            cred = runtime.resolver.resolve_account(account)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = await probe_account(runtime.snap(), cred)
        return {"account_id": account_id, "name": account.name, **result}

    @protected.post("/admin/accounts/{account_id}/login")
    async def post_admin_account_login(
        account_id: str,
        request: Request,
        current_settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        return _start_login(_state(request), account_id, current_settings.oauth_callback_port)

    @protected.get("/admin/accounts/{account_id}/login")
    async def get_admin_account_login_status(account_id: str, request: Request) -> dict[str, Any]:
        runtime = _state(request)
        account = runtime.pool.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
        payload = _login_status_payload(runtime)
        if payload.get("status") == "pending" and runtime.login_account_id not in {account_id, None}:
            payload = {
                **payload,
                "message": "当前正在为另一个账号等待 OAuth 回调。",
            }
        return payload

    app.include_router(protected)
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


async def _stream_chat(runtime: AppState, payload: dict[str, Any], model: str):
    try:
        stream_cm, response = await open_chat_stream_with_failover(
            client=runtime.snap(),
            resolver=runtime.resolver,
            pool=runtime.pool,
            payload=payload,
        )
    except UpstreamError as exc:
        yield format_sse_json(openai_error(str(exc), exc.status_code))
        yield format_sse_done()
        return
    except httpx.HTTPError as exc:
        logger.exception("Streaming chat failed to start")
        yield format_sse_json(openai_error(f"Upstream stream error: {exc}", 502))
        yield format_sse_done()
        return

    try:
        buffer = ""
        saw_done = False
        async for text in response.aiter_text():
            buffer += text
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if line == "":
                    continue
                if line.startswith(":"):
                    continue
                data = line
                if line.startswith("data:"):
                    data = line[5:].lstrip()
                elif line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
                    continue
                if data.strip() == "[DONE]":
                    saw_done = True
                    yield format_sse_done()
                    continue
                try:
                    parsed = json.loads(data)
                except ValueError:
                    yield f"data: {data}\n\n"
                    continue
                yield format_sse_json(normalize_chunk(parsed, model))
        leftover = buffer.strip()
        if leftover:
            if leftover.startswith("data:"):
                leftover = leftover[5:].lstrip()
            if leftover == "[DONE]":
                saw_done = True
                yield format_sse_done()
            else:
                try:
                    parsed = json.loads(leftover)
                    yield format_sse_json(normalize_chunk(parsed, model))
                except ValueError:
                    yield f"data: {leftover}\n\n"
        if not saw_done:
            yield format_sse_done()
    except httpx.HTTPError as exc:
        logger.exception("Streaming chat failed")
        yield format_sse_json(openai_error(f"Upstream stream error: {exc}", 502))
        yield format_sse_done()
    finally:
        try:
            await stream_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to close chat stream", exc_info=True)


# Default app instance for ``uvicorn codearts2api.app:app`` / ``python -m codearts2api``.
app = create_app()
