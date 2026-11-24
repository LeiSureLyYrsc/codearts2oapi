"""FastAPI application: OpenAI-compatible gateway, account pool, and WebUI."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
from codearts2api.oauth.flow import (
    CALLBACK_TIMEOUT_SECONDS,
    CallbackServer,
    LoginError,
    build_authorize_url,
    normalize_manual_callback,
)
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
from codearts2api.oauth.session import SESSION_FILENAME
from codearts2api.schemas import (
    AccountCreateRequest,
    AccountUpdateRequest,
    ChatCompletionsRequest,
    CheckinRequest,
    OAuthLoginStartRequest,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Retain finished login results so status polling and a duplicate submit never
# consume each other's success, then forget them.
LOGIN_RESULT_TTL_SECONDS = 900.0
LOGIN_RESULTS_MAX = 16
LOGIN_BODY_LIMIT = 16 * 1024
LOGIN_EXCHANGE_ERROR = "换取凭证失败：授权码可能已失效或已被使用，请重新开始授权。"
LOGIN_WRITE_ERROR = "无法写入本机凭证文件，请检查数据目录权限后重试。"
LOGIN_NAME_TAKEN = "已存在同名账号，请换一个名称后重新开始授权。"
LOGIN_CALLBACK_INVALID = (
    "回调地址无效：请粘贴以 http://127.0.0.1:<端口>/oauth/callback? 开头的完整地址，"
    "并包含一个 code 或 secret 参数。"
)
LOGIN_CALLBACK_HINT = (
    "在能访问本机回环地址的浏览器中打开 authorize_url 完成授权；"
    "若回调页面打不开，请把浏览器地址栏里的完整回调地址粘贴回本页。"
)


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


@dataclass
class LoginAttempt:
    """One live in-process OAuth attempt, addressed by ``login_id``.

    Holds the listener and PKCE state, so it only lives while pending. Once it
    settles it is replaced by a :class:`LoginResult` in the finished cache.
    """

    login_id: str
    account_id: str | None
    name: str
    note: str
    enabled: bool
    legacy: bool
    server: CallbackServer
    authorize_url: str
    created_at: float
    message: str | None = None
    timer: Any = None


@dataclass(frozen=True)
class LoginResult:
    """Safe terminal snapshot: no listener, PKCE state, or upstream error text."""

    login_id: str
    status: str
    account_id: str | None
    message: str | None
    settled_at: float


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
        self.login_active: LoginAttempt | None = None
        self.login_finished: dict[str, LoginResult] = {}
        self.login_last: str | None = None

    def snap(self) -> SnapAccessClient:
        if self.client is None:
            raise HTTPException(status_code=503, detail="HTTP client is not ready")
        return self.client


def _state(request: Request) -> AppState:
    return request.app.state.runtime


DEFAULT_OAUTH_NAME = "oauth"


def _legacy_session_account(pool: AccountPool) -> Any:
    """The account already bound to the legacy session.json file, if any."""
    for account in pool.accounts:
        if account.kind == "oauth" and (account.session_file or "") == SESSION_FILENAME:
            return account
    return None


def _free_oauth_name(pool: AccountPool) -> str:
    """A default OAuth name that does not collide with a non-OAuth account."""
    for candidate in [DEFAULT_OAUTH_NAME, *(f"{DEFAULT_OAUTH_NAME}-{i}" for i in range(2, 100))]:
        existing = pool.find_by_name(candidate)
        if existing is None or existing.kind == "oauth":
            return candidate
    return f"{DEFAULT_OAUTH_NAME}-{uuid.uuid4().hex[:8]}"


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
            active = runtime.login_active
            runtime.login_active = None
            if active is not None:
                if active.timer is not None:
                    active.timer.cancel()
                    active.timer = None
                await asyncio.to_thread(active.server.stop)
            runtime.login_finished.clear()
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
            try:
                stream_cm, response = await open_chat_stream_with_failover(
                    client=runtime.snap(),
                    resolver=runtime.resolver,
                    pool=runtime.pool,
                    payload=upstream_payload,
                )
            except UpstreamError as exc:
                status = exc.status_code if 400 <= exc.status_code < 600 else 502
                return JSONResponse(
                    status_code=status,
                    content=openai_error(str(exc), status),
                )
            except httpx.HTTPError:
                return JSONResponse(
                    status_code=502,
                    content=openai_error("Upstream stream connection failed", 502),
                )
            content_type = response.headers.get("content-type", "").lower()
            if "text/event-stream" not in content_type and (
                "application/json" in content_type or "text/html" in content_type
            ):
                try:
                    raw = bytearray()
                    if "json" in content_type:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            if len(raw) + len(chunk) > 64 * 1024:
                                raw.clear()
                                break
                            raw.extend(chunk)
                    parsed = json.loads(raw) if raw else None
                except (httpx.HTTPError, ValueError):
                    parsed = None
                finally:
                    try:
                        await stream_cm.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001
                        logger.debug("Failed to close non-stream chat response", exc_info=True)
                return JSONResponse(
                    status_code=502,
                    content=openai_error(
                        _safe_upstream_error(parsed) or "Upstream returned a non-stream response", 502
                    ),
                )
            return StreamingResponse(
                _stream_chat(stream_cm, response, model),
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

    # ---- OAuth login attempts (all mutations run on the ASGI event loop) ----

    def _prune_logins(runtime: AppState) -> None:
        cutoff = time.monotonic() - LOGIN_RESULT_TTL_SECONDS
        for login_id, result in list(runtime.login_finished.items()):
            if result.settled_at < cutoff:
                runtime.login_finished.pop(login_id, None)
        while len(runtime.login_finished) > LOGIN_RESULTS_MAX:
            oldest = min(runtime.login_finished.values(), key=lambda item: item.settled_at)
            runtime.login_finished.pop(oldest.login_id, None)

    def _retire_login(
        runtime: AppState,
        attempt: LoginAttempt,
        *,
        status: str,
        message: str | None,
        account_id: str | None = None,
    ) -> LoginResult:
        """Stop the listener and keep only a safe terminal snapshot."""
        if runtime.login_active is attempt:
            runtime.login_active = None
        if attempt.timer is not None:
            attempt.timer.cancel()
            attempt.timer = None
        result = LoginResult(
            login_id=attempt.login_id,
            status=status,
            account_id=account_id or attempt.account_id,
            message=message,
            settled_at=time.monotonic(),
        )
        # Publish the result before the (blocking) listener shutdown so the
        # attempt is never observably both inactive and unrecorded.
        runtime.login_finished[attempt.login_id] = result
        runtime.login_last = attempt.login_id
        _prune_logins(runtime)
        attempt.server.stop()
        return result

    def _schedule_expiry(runtime: AppState, attempt: LoginAttempt) -> None:
        """Abandon the attempt on the loop even if no request ever arrives again."""
        loop = asyncio.get_running_loop()

        def expire() -> None:
            if runtime.login_active is not attempt:
                return
            _retire_login(
                runtime,
                attempt,
                status="error",
                message="授权等待超时，请重新开始。",
            )
            logger.info("OAuth attempt expired login_id=%s", attempt.login_id)

        attempt.timer = loop.call_later(CALLBACK_TIMEOUT_SECONDS, expire)

    def _expire_if_stale(runtime: AppState, attempt: LoginAttempt) -> None:
        """Synchronous fallback in case the loop timer was skipped (e.g. blocked loop)."""
        if (
            runtime.login_active is attempt
            and not attempt.server.done
            # >= because time.monotonic() is coarse (~15ms on Windows) and a
            # test or config may set the timeout to 0.
            and time.monotonic() - attempt.created_at >= CALLBACK_TIMEOUT_SECONDS
        ):
            _retire_login(
                runtime,
                attempt,
                status="error",
                message="授权等待超时，请重新开始。",
            )

    def _commit_login(runtime: AppState, attempt: LoginAttempt, session: Any) -> str:
        if attempt.account_id:
            account = runtime.pool.get(attempt.account_id)
            if account is None:
                raise LoginError("账号已在授权期间被删除")
            # Reauth only replaces the stored session after a successful save.
            runtime.pool.attach_session(account, session)
            return account.id
        if attempt.legacy:
            # Legacy GET /admin/login keeps its old behavior: reuse the account
            # bound to session.json, otherwise create the conventional one.
            if _legacy_session_account(runtime.pool) is not None:
                account = runtime.pool.find_or_create_oauth(use_legacy_session=True)
            else:
                clash = runtime.pool.find_by_name(DEFAULT_OAUTH_NAME)
                if clash is not None and clash.kind != "oauth":
                    # Never take over an AK/SK account that happens to be called
                    # "oauth"; fall back to a free name instead.
                    account = runtime.pool.find_or_create_oauth(name=_free_oauth_name(runtime.pool))
                else:
                    account = runtime.pool.find_or_create_oauth(use_legacy_session=True)
            runtime.pool.attach_session(account, session)
            return account.id
        account = runtime.pool.create_oauth_account(
            name=attempt.name,
            session=session,
            enabled=attempt.enabled,
            note=attempt.note,
        )
        return account.id

    def _settle_active(runtime: AppState) -> None:
        """Commit the active attempt once its callback exchange has finished."""
        attempt = runtime.login_active
        if attempt is None or not attempt.server.done:
            return
        session = attempt.server.result
        error = attempt.server.error
        if error is not None or session is None:
            _retire_login(runtime, attempt, status="error", message=LOGIN_EXCHANGE_ERROR)
            # Log the type only: upstream text can echo the submitted code.
            logger.warning(
                "OAuth login failed login_id=%s error=%s",
                attempt.login_id,
                type(error).__name__ if error is not None else "missing-session",
            )
            return
        try:
            account_id = _commit_login(runtime, attempt, session)
        except ValueError:
            _retire_login(runtime, attempt, status="error", message=LOGIN_NAME_TAKEN)
            logger.warning("OAuth login commit rejected login_id=%s", attempt.login_id)
            return
        except Exception:  # noqa: BLE001
            _retire_login(runtime, attempt, status="error", message=LOGIN_WRITE_ERROR)
            logger.exception("OAuth login commit failed login_id=%s", attempt.login_id)
            return
        _retire_login(runtime, attempt, status="logged_in", message=None, account_id=account_id)
        runtime.model_cache.clear()
        refresh_logging(runtime.settings, runtime.pool)
        logger.info("OAuth login completed login_id=%s account_id=%s", attempt.login_id, account_id)

    def _lookup_finished(runtime: AppState, login_id: str) -> LoginResult | None:
        result = runtime.login_finished.get(login_id)
        if result is None:
            return None
        if result.settled_at < time.monotonic() - LOGIN_RESULT_TTL_SECONDS:
            runtime.login_finished.pop(login_id, None)
            return None
        return result


    def _pending_payload(runtime: AppState, attempt: LoginAttempt) -> dict[str, Any]:
        return {
            "login_id": attempt.login_id,
            "status": "pending",
            "account_id": attempt.account_id,
            "authorize_url": attempt.authorize_url,
            "callback_url": attempt.server.callback_url,
            "client_id": runtime.resolver.endpoints().client_id,
            "message": attempt.message or LOGIN_CALLBACK_HINT,
        }

    def _result_payload(runtime: AppState, result: LoginResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "login_id": result.login_id,
            "status": result.status,
            "account_id": result.account_id,
        }
        if result.message:
            payload["message"] = result.message
        if result.status == "logged_in":
            payload["session"] = runtime.resolver.status()
        return payload

    def _start_login(
        runtime: AppState,
        *,
        account_id: str | None,
        name: str,
        note: str,
        enabled: bool,
        legacy: bool,
        callback_port: int,
        resume_active: bool,
    ) -> dict[str, Any]:
        _prune_logins(runtime)
        active = runtime.login_active
        if active is not None:
            # Deadline first: an expired attempt must not be settled as success.
            _expire_if_stale(runtime, active)
            active = runtime.login_active
        if active is not None:
            _settle_active(runtime)
            active = runtime.login_active
        if active is not None:
            same_target = active.account_id == account_id and (
                account_id is not None or active.name == name
            )
            if resume_active and same_target:
                return _pending_payload(runtime, active)
            if legacy and active.legacy:
                # Legacy GET historically returned the waiting legacy flow.
                return _pending_payload(runtime, active)
            raise HTTPException(
                status_code=409,
                detail="已有另一次授权正在进行，请先完成或取消它，再开始新的授权。",
            )

        if account_id:
            account = runtime.pool.get(account_id)
            if account is None:
                raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
            if account.kind != "oauth":
                raise HTTPException(
                    status_code=400,
                    detail="OAuth login can only be started for oauth accounts",
                )
            name = account.name
        else:
            name = (name or "").strip()
            if not name:
                raise HTTPException(
                    status_code=400,
                    detail="请填写账号名称，用于在账号池中区分这个 OAuth 账号。",
                )
            if not legacy:
                # A modern kickoff always creates a new account, so a name clash
                # (including a legacy OAuth account called "oauth") is reported
                # instead of silently resuming someone else's flow.
                clash = runtime.pool.find_by_name(name)
                if clash is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"已存在同名账号「{name}」，请换一个名称，或先删除旧账号。",
                    )

        endpoints = runtime.resolver.endpoints()
        pkce = generate_pkce_pair()
        ticket_id = generate_ticket_id()
        dpop = generate_p256_keypair()
        try:
            server = CallbackServer(endpoints, port=callback_port)
            server.start(pkce, ticket_id, dpop)
        except OSError as exc:
            logger.warning("Cannot start OAuth callback listener (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail=(
                    "无法启动本机回调端口，可能已被占用。"
                    "请关闭占用该端口的程序，或修改 oauth_callback_port 后重试。"
                ),
            ) from None
        authorize_url = build_authorize_url(
            endpoints,
            port=server.port,
            code_challenge=pkce.code_challenge,
            ticket_id=ticket_id,
            code_challenge_method=pkce.code_challenge_method,
        )
        attempt = LoginAttempt(
            login_id=uuid.uuid4().hex,
            account_id=account_id,
            name=name,
            note=note or "",
            enabled=bool(enabled),
            legacy=legacy,
            server=server,
            authorize_url=authorize_url,
            created_at=time.monotonic(),
        )
        runtime.login_active = attempt
        _schedule_expiry(runtime, attempt)
        return _pending_payload(runtime, attempt)


    @protected.post("/admin/login/start")
    async def post_admin_login_start(
        body: OAuthLoginStartRequest,
        request: Request,
        current_settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        """Begin a new-account OAuth flow; the account is written on success."""
        return _start_login(
            _state(request),
            account_id=None,
            name=body.name,
            note=body.note,
            enabled=body.enabled,
            legacy=False,
            callback_port=current_settings.oauth_callback_port,
            resume_active=False,
        )

    @protected.get("/admin/login")
    async def get_admin_login(
        request: Request,
        account_id: str | None = Query(default=None),
        current_settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        """Legacy entry point: start (or resume) the official CLI loopback OAuth flow."""
        if account_id:
            return _start_login(
                _state(request),
                account_id=account_id,
                name="",
                note="",
                enabled=True,
                legacy=False,
                callback_port=current_settings.oauth_callback_port,
                resume_active=False,
            )
        return _start_login(
            _state(request),
            account_id=None,
            name=DEFAULT_OAUTH_NAME,
            note="",
            enabled=True,
            legacy=True,
            callback_port=current_settings.oauth_callback_port,
            resume_active=True,
        )

    @protected.get("/admin/login/status")
    async def get_admin_login_status(
        request: Request,
        login_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        runtime = _state(request)
        _prune_logins(runtime)
        if login_id:
            active = runtime.login_active
            if active is not None and active.login_id == login_id:
                # Only the addressed attempt is settled; a stale ID never
                # touches (or commits) a different in-flight flow.
                _settle_active(runtime)
                active = runtime.login_active
                if active is not None and active.login_id == login_id:
                    _expire_if_stale(runtime, active)
                    if runtime.login_active is active:
                        return _pending_payload(runtime, active)
            result = _lookup_finished(runtime, login_id)
            if result is None:
                return {
                    "status": "idle",
                    "account_id": None,
                    "message": "本次授权会话已结束或已过期，请重新开始。",
                }
            return _result_payload(runtime, result)
        active = runtime.login_active
        if active is not None:
            _expire_if_stale(runtime, active)
            active = runtime.login_active
        if active is not None and active.legacy:
            # Legacy flow: the unscoped poll owns it, as before.
            _settle_active(runtime)
            active = runtime.login_active
        if active is not None:
            if active.legacy:
                return _pending_payload(runtime, active)
            # Unscoped status is legacy-compatible only: never hand out a modern
            # flow's login_id (a capability to poll URLs or cancel) or its URLs.
            return {
                "status": "pending",
                "account_id": None,
                "message": "已有另一次授权正在进行，请回到发起授权的页面继续。",
            }
        last = _lookup_finished(runtime, runtime.login_last) if runtime.login_last else None
        if last is not None:
            return _result_payload(runtime, last)
        return {
            "status": "idle",
            "account_id": None,
            "session": runtime.resolver.status(),
        }

    @protected.post("/admin/login/callback")
    async def post_admin_login_callback(request: Request) -> dict[str, Any]:
        """Accept a pasted loopback callback URL; it is validated, never fetched.

        The body is parsed by hand so a validation failure cannot echo the raw
        URL (a pydantic 422 would include the submitted input).
        """
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > LOGIN_BODY_LIMIT:
                raise HTTPException(status_code=413, detail="回调请求内容过大。")
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeError):
            raise HTTPException(status_code=400, detail=LOGIN_CALLBACK_INVALID) from None
        login_id = body.get("login_id") if isinstance(body, dict) else None
        callback_url = body.get("callback_url") if isinstance(body, dict) else None
        if not isinstance(login_id, str) or not isinstance(callback_url, str):
            raise HTTPException(status_code=400, detail=LOGIN_CALLBACK_INVALID)

        runtime = _state(request)
        _prune_logins(runtime)
        active = runtime.login_active
        if active is None or active.login_id != login_id:
            # A settled ID returns its terminal result without touching the URL;
            # an unknown or cancelled ID cannot be retried.
            result = _lookup_finished(runtime, login_id)
            if result is not None:
                return _result_payload(runtime, result)
            raise HTTPException(
                status_code=400,
                detail="本次授权会话已结束或已过期，请重新开始授权。",
            )
        # Address-based: only this attempt may be settled, never a newer one.
        _settle_active(runtime)
        active = runtime.login_active
        if active is None or active.login_id != login_id:
            result = _lookup_finished(runtime, login_id)
            if result is not None:
                return _result_payload(runtime, result)
            raise HTTPException(status_code=400, detail="本次授权会话已结束，请重新开始授权。")
        _expire_if_stale(runtime, active)
        if runtime.login_active is not active:
            result = _lookup_finished(runtime, login_id)
            if result is not None:
                return _result_payload(runtime, result)
            raise HTTPException(status_code=400, detail="本次授权会话已结束，请重新开始授权。")

        try:
            carrier = normalize_manual_callback(callback_url, expected_port=active.server.port)
        except ValueError:
            # Static message: the submitted URL is neither echoed nor logged.
            raise HTTPException(status_code=400, detail=LOGIN_CALLBACK_INVALID) from None
        try:
            active.server.submit_manual(carrier)
        except ValueError:
            # A browser callback or an earlier submit already owns this attempt.
            return _pending_payload(runtime, active)
        active.message = "回调已收到，正在换取凭证…"
        logger.info("OAuth manual callback accepted login_id=%s", active.login_id)
        return _pending_payload(runtime, active)

    @protected.delete("/admin/login")
    async def delete_admin_login(
        request: Request,
        login_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        runtime = _state(request)
        if not login_id:
            # Cancellation is addressed by login_id only; there is no global cancel.
            raise HTTPException(status_code=400, detail="请提供 login_id 以取消对应的授权。")
        _prune_logins(runtime)
        active = runtime.login_active
        if active is not None and active.login_id == login_id:
            # Cancel without settling: a result that arrives later is discarded,
            # and a finished exchange cannot slip a write in during cancel.
            _retire_login(runtime, active, status="idle", message="本次授权已取消。")
            logger.info("OAuth login cancelled login_id=%s", login_id)
            return {"ok": True, "login_id": login_id, "status": "idle"}
        result = _lookup_finished(runtime, login_id)
        if result is not None:
            return {"ok": True, "login_id": login_id, "status": result.status}
        return {"ok": True, "login_id": login_id, "status": "idle"}


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
        if body.kind == "oauth":
            # OAuth credentials only ever come from the browser/ticket exchange;
            # even a supplied ak/sk would be ignored by the resolver, so a direct
            # create would silently yield an unusable account.
            raise HTTPException(
                status_code=400,
                detail=(
                    "OAuth 账号的凭证来自浏览器授权，不能直接创建。"
                    "请使用 POST /admin/login/start 开始授权，成功后再写入账号池；"
                    "若要迁移已有账号，请导出 Auth JSON 并通过 POST /admin/accounts/import 导入。"
                ),
            )
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
        """Reauthorize an existing OAuth account in place."""
        return _start_login(
            _state(request),
            account_id=account_id,
            name="",
            note="",
            enabled=True,
            legacy=False,
            callback_port=current_settings.oauth_callback_port,
            resume_active=True,
        )

    @protected.get("/admin/accounts/{account_id}/login")
    async def get_admin_account_login_status(account_id: str, request: Request) -> dict[str, Any]:
        runtime = _state(request)
        account = runtime.pool.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
        _prune_logins(runtime)
        _settle_active(runtime)
        active = runtime.login_active
        if active is not None:
            _expire_if_stale(runtime, active)
            if runtime.login_active is active:
                if active.account_id == account_id:
                    return _pending_payload(runtime, active)
                # Never leak another target's login_id or in-progress URLs.
                return {
                    "status": "pending",
                    "account_id": None,
                    "message": "当前正在为另一个账号等待 OAuth 回调。",
                }
        for login_id in reversed(list(runtime.login_finished)):
            result = runtime.login_finished[login_id]
            if result.account_id == account_id:
                return _result_payload(runtime, result)
        return {"status": "idle", "account_id": account_id}

    app.include_router(protected)
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


def _safe_upstream_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    # Follow the conventional OpenAI error.message first; never return an opaque
    # upstream envelope (which can contain request details or credentials).
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:1000]
    if isinstance(payload.get("error_msg"), str):
        code = payload.get("error_code")
        return f'{payload["error_msg"][:1000]} (code {code})' if code is not None else payload["error_msg"][:1000]
    if isinstance(payload.get("message"), str):
        return payload["message"][:1000]
    return None


async def _stream_chat(stream_cm: Any, response: httpx.Response, model: str):
    metadata: dict[str, Any] = {}
    finished: dict[int, str | None] = {}
    tool_choices: set[int] = set()
    function_choices: set[int] = set()
    data_lines: list[str] = []

    def process_data(data: str) -> tuple[str | None, dict[str, Any] | None]:
        data = data.strip()
        if data == "[DONE]":
            return "done", None
        try:
            parsed = json.loads(data)
        except ValueError:
            return "malformed", None
        if isinstance(parsed, dict) and (
            "error" in parsed or parsed.get("error_msg") or parsed.get("error_code")
        ):
            return "error", parsed
        normalized = normalize_chunk(parsed, model)
        if isinstance(normalized.get("id"), str):
            metadata.setdefault("id", normalized["id"])
        if isinstance(normalized.get("created"), int):
            metadata.setdefault("created", normalized["created"])
        if isinstance(normalized.get("model"), str):
            metadata.setdefault("model", normalized["model"])
        choices = normalized.get("choices")
        if not isinstance(choices, list):
            return "malformed", None
        for choice in choices:
            if not isinstance(choice, dict):
                return "malformed", None
            idx = choice.get("index", 0)
            if not isinstance(idx, int):
                return "malformed", None
            finished.setdefault(idx, None)
            delta = choice.get("delta")
            if delta is None and choice.get("finish_reason") is not None:
                choice["delta"] = delta = {}
            if not isinstance(delta, dict):
                return "malformed", None
            if delta.get("tool_calls"):
                tool_choices.add(idx)
            if delta.get("function_call"):
                function_choices.add(idx)
            if choice.get("finish_reason") is not None:
                finished[idx] = choice["finish_reason"]
        return "chunk", normalized

    def inferred_finish(index: int) -> str:
        if index in function_choices:
            return "function_call"
        if index in tool_choices:
            return "tool_calls"
        return "stop"

    async def close_stream() -> None:
        try:
            await asyncio.shield(stream_cm.__aexit__(None, None, None))
        except Exception:  # noqa: BLE001
            logger.debug("Failed to close chat stream", exc_info=True)

    try:
        def consume_data(data: str) -> tuple[list[str], bool]:
            result, value = process_data(data)
            if result == "done":
                if not finished:
                    return [format_sse_json(openai_error("Upstream sent [DONE] without any choices", 502))], True
                output = []
                for idx, reason in finished.items():
                    if reason is None:
                        chunk = {**metadata, "object": "chat.completion.chunk", "choices": [
                            {"index": idx, "delta": {}, "finish_reason": inferred_finish(idx)}
                        ]}
                        output.append(format_sse_json(chunk))
                output.append(format_sse_done())
                return output, True
            if result == "malformed":
                return [format_sse_json(openai_error("Malformed upstream stream data", 502))], True
            if result == "error":
                message = _safe_upstream_error(value) or "Upstream stream returned an error"
                return [format_sse_json(openai_error(message, 502))], True
            return [format_sse_json(value)], False

        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if line == "":
                if data_lines:
                    data = "\n".join(data_lines)
                    data_lines.clear()
                    output, stop = consume_data(data)
                else:
                    continue
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
                continue
            elif line.startswith((":", "event:", "id:", "retry:")):
                continue
            elif not data_lines:
                # Mislabeled NDJSON: process each bare record independently,
                # including records separated by blank lines.
                output, stop = consume_data(line)
            else:
                # Non-SSE text inside a pending SSE event is not part of its data.
                continue
            for item in output:
                yield item
            if stop:
                return
        if data_lines:
            output, stop = consume_data("\n".join(data_lines))
            for item in output:
                yield item
            if stop:
                return
        if finished and all(reason is not None for reason in finished.values()):
            yield format_sse_done()
        else:
            yield format_sse_json(openai_error("Upstream stream ended before completion", 502))
    except httpx.HTTPError:
        logger.warning("Upstream chat stream interrupted")
        yield format_sse_json(openai_error("Upstream stream interrupted", 502))
    finally:
        await close_stream()


# Default app instance for ``uvicorn codearts2api.app:app`` / ``python -m codearts2api``.
app = create_app()
