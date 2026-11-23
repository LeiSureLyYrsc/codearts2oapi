"""Signed snap-access HTTP client (async httpx)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from codearts2api.config import Settings
from codearts2api.credentials import CredentialResolver, ResolvedCredential
from codearts2api.signing import sign_request

logger = logging.getLogger(__name__)

PLUGIN_STATISTICS_PATH = "/snap-manager/v1/statistics/plugin"
MODELS_PATH = "/v1/model/builtin"
CHAT_PATH = "/api/v2/chat/completions"
DELIVERY_PATH = "/v1/ops/delivery"
CLAIM_PATH = "/v1/ops/claim"
CONFIRM_PATH = "/v1/ops/confirm"
BENEFIT_CLAIM_PATH = "/api/v1/benefit/claim"
CHECKIN_CHANNEL = "DESKTOP"


def json_body(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class UpstreamError(Exception):
    def __init__(self, message: str, status_code: int, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SnapAccessClient:
    """Calls the official-client snap-access gateway with SDK-HMAC-SHA256."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        resolver: CredentialResolver | None = None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._resolver = resolver or CredentialResolver(settings)

    def _credential(self, credential: ResolvedCredential | None = None) -> ResolvedCredential:
        if credential is not None:
            return credential
        try:
            return self._resolver.resolve()
        except RuntimeError as exc:
            raise UpstreamError(str(exc), 503) from exc

    def _unsigned_headers(self) -> dict[str, str] | None:
        language = self._settings.x_language
        if language:
            return {"X-Language": language}
        return None

    def bind(self, credential: ResolvedCredential) -> "BoundSnapClient":
        return BoundSnapClient(self, credential)

    def _build_url(self, path: str, query: dict[str, str] | None = None) -> str:
        base = self._settings.snap_base_url.rstrip("/") + "/"
        url = urljoin(base, path.lstrip("/"))
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        return url

    def sign(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        credential: ResolvedCredential | None = None,
        *,
        sign_language: bool = False,
    ) -> dict[str, str]:
        cred = self._credential(credential)
        outgoing = dict(headers or {})
        if body and not any(k.lower() == "content-type" for k in outgoing):
            outgoing["Content-Type"] = "application/json"
        if sign_language:
            outgoing["X-Language"] = self._settings.x_language or "zh-cn"
        project_id = cred.project_id if cred.project_id is not None else self._settings.codearts_project_id
        signed = sign_request(
            method=method,
            url=url,
            ak=cred.access_key_id,
            sk=cred.secret_access_key,
            headers=outgoing,
            body=body,
            security_token=cred.security_token,
            project_id=project_id,
            extra_unsigned_headers=None if sign_language else self._unsigned_headers(),
            sign_language=sign_language,
        )
        return signed.headers

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        payload: Any = None,
        body: bytes | None = None,
        expected: tuple[int, ...] = (200, 201),
        credential: ResolvedCredential | None = None,
        headers: dict[str, str] | None = None,
        sign_language: bool = False,
    ) -> tuple[Any, int]:
        if body is None and payload is not None:
            body = json_body(payload)
        url = self._build_url(path, query)
        cred = self._credential(credential)
        signed = self.sign(method, url, body=body, headers=headers, credential=cred, sign_language=sign_language)
        label = cred.account_id or cred.source
        logger.info("upstream %s %s account=%s", method.upper(), path, label)
        response = await self._http.request(
            method.upper(),
            url,
            headers=signed,
            content=body if body is not None else None,
        )
        parsed = _parse_body(response)
        if response.status_code not in expected:
            message = _error_message(parsed, response.status_code)
            raise UpstreamError(message, response.status_code, parsed)
        return parsed, response.status_code

    async def get_plugin_statistics(self, credential: ResolvedCredential | None = None) -> Any:
        payload, _status = await self.request_json(
            "GET", PLUGIN_STATISTICS_PATH, headers={"Content-Type": "application/json"}, credential=credential,
        )
        return payload

    @staticmethod
    def _prompt_center_headers() -> dict[str, str]:
        return {"Agent-Type": "PromptCenter", "Content-Type": "application/json"}

    async def list_builtin_models(self, credential: ResolvedCredential | None = None) -> Any:
        payload, _status = await self.request_json(
            "GET", MODELS_PATH, headers=self._prompt_center_headers(), sign_language=True, credential=credential,
        )
        return payload

    async def list_activities(self, channel: str = CHECKIN_CHANNEL, credential: ResolvedCredential | None = None) -> Any:
        payload, _status = await self.request_json(
            "GET",
            DELIVERY_PATH,
            query={"channel": channel},
            headers={**self._prompt_center_headers(), "x-snap-traceid": str(uuid.uuid4())},
            sign_language=True,
            credential=credential,
        )
        return payload

    async def claim_campaign(
        self,
        campaign_id: Any,
        channel: str = CHECKIN_CHANNEL,
        credential: ResolvedCredential | None = None,
    ) -> tuple[Any, int]:
        return await self.request_json(
            "POST",
            CLAIM_PATH,
            payload={"campaignId": campaign_id, "idempotentKey": f"claim_{uuid.uuid4().hex}", "channel": channel},
            expected=tuple(range(200, 300)) + tuple(range(400, 500)),
            headers=self._prompt_center_headers(),
            sign_language=True,
            credential=credential,
        )

    async def confirm_campaign(
        self,
        user_benefit_id: Any,
        credential: ResolvedCredential | None = None,
    ) -> tuple[Any, int]:
        return await self.request_json(
            "POST",
            CONFIRM_PATH,
            payload={"userBenefitId": user_benefit_id},
            expected=tuple(range(200, 300)) + tuple(range(400, 500)),
            headers=self._prompt_center_headers(),
            sign_language=True,
            credential=credential,
        )

    async def post_chat(self, payload: dict[str, Any], credential: ResolvedCredential | None = None) -> Any:
        body, _status = await self.request_json("POST", CHAT_PATH, payload=payload, credential=credential)
        return body

    def stream_chat(self, payload: dict[str, Any], credential: ResolvedCredential | None = None) -> httpx.Response:
        """Return an async context manager for a signed streaming POST."""
        body = json_body(payload)
        url = self._build_url(CHAT_PATH)
        cred = self._credential(credential)
        headers = self.sign("POST", url, body=body, credential=cred)
        headers.setdefault("Accept", "text/event-stream")
        timeout = httpx.Timeout(
            connect=self._settings.http_timeout,
            read=self._settings.http_stream_timeout,
            write=self._settings.http_timeout,
            pool=self._settings.http_timeout,
        )
        label = cred.account_id or cred.source
        logger.info("upstream POST %s (stream) account=%s", CHAT_PATH, label)
        return self._http.stream("POST", url, headers=headers, content=body, timeout=timeout)

    async def claim_opengw_benefit(self, credential: ResolvedCredential | None = None) -> dict[str, Any]:
        """Limited-time model benefit claim, as configured in the official CLI.

        Official CLI 26.11.11 (China production) uses
        POST https://opengw.developer.huaweicloud.com/api/v1/benefit/claim
        with an empty JSON object, SDK-HMAC-SHA256, optional X-Security-Token,
        and treats error_code == "0000" as success.
        """
        base = self._settings.opengw_base_url
        if not base:
            return {
                "status": "skipped",
                "reason": "OPENGW_BASE_URL is empty.",
            }
        url = base.rstrip("/") + BENEFIT_CLAIM_PATH
        body = json_body({})
        extra = {"x-snap-traceid": uuid.uuid4().hex}
        headers = self.sign("POST", url, body=body, headers=extra, credential=credential)
        response = await self._http.post(url, headers=headers, content=body)
        parsed = _parse_body(response)
        code = None
        if isinstance(parsed, dict):
            code = parsed.get("error_code") or parsed.get("errorCode") or parsed.get("code")
        success = str(code) == "0000"
        return {
            "status": "claimed" if success else "error",
            "http_status": response.status_code,
            "error_code": code,
            "payload": parsed if isinstance(parsed, (dict, list)) else {"raw": str(parsed)},
        }



class BoundSnapClient:
    """SnapAccessClient bound to one ResolvedCredential (check-in / per-account calls)."""

    def __init__(self, client: SnapAccessClient, credential: ResolvedCredential) -> None:
        self._client = client
        self._credential = credential

    async def get_plugin_statistics(self) -> Any:
        return await self._client.get_plugin_statistics(credential=self._credential)

    async def list_activities(self, channel: str = CHECKIN_CHANNEL) -> Any:
        return await self._client.list_activities(channel=channel, credential=self._credential)

    async def claim_campaign(self, campaign_id: Any, channel: str = CHECKIN_CHANNEL) -> tuple[Any, int]:
        return await self._client.claim_campaign(campaign_id, channel=channel, credential=self._credential)

    async def confirm_campaign(self, user_benefit_id: Any) -> tuple[Any, int]:
        return await self._client.confirm_campaign(user_benefit_id, credential=self._credential)

    async def claim_opengw_benefit(self) -> dict[str, Any]:
        return await self._client.claim_opengw_benefit(credential=self._credential)

    async def list_builtin_models(self) -> Any:
        return await self._client.list_builtin_models(credential=self._credential)


def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type or response.content[:1] in (b"{", b"["):
        try:
            return response.json()
        except ValueError:
            return response.text
    return response.text


def _error_message(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error_msg", "errorMsg", "msg", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"]
    return f"Upstream request failed with HTTP {status_code}"
