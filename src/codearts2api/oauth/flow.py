"""Official CodeArts CLI browser SSO flow.

Protocol details were read from the official Huawei Cloud 码道 CLI 26.11.11
package (`agentkernel`), downloaded from the official install channel:

https://cnnorth4-cloudide-marketplace.obs.cn-north-4.myhuaweicloud.com/codearts/cli_tui/install_script/install.sh
https://codearts-agent-obs-cdn.huaweicloud.com/codearts/cli_tui/latest/codearts-install-26.11.11-linux-x64.tar.gz

They are not published in the public CodeArts Agent API catalog. Official
user guides only describe "open a browser, log in, confirm authorization".

Observed official CLI constants and behavior (China station, production):

- Portal: https://codearts.huaweicloud.com/portal
- Authorize: GET {portal}/authorize?locale=&client_id=&port=&code_challenge=&code_challenge_method=SHA-256&ticket_id=
- Official CLI client_id: CodeArts_Tui  (configurable; other official clients may differ)
- Local callback: http://127.0.0.1:<port>/oauth/callback  (port 0 = OS assigned)
- Callback query: code and/or secret+redirect. A request with neither is a health check (200).
  secret+redirect: 307 to redirect, then poll
  GET {snap-manager}/v1/login/ticket?ticket_id=&secret=  up to 10 times, 1s apart
- Code exchange: POST {sts}/v1/oauth2/tokens
  form: client_id, code, code_verifier, grant_type=authorization_code, redirect_uri
  header DPoP: ES256 dpop+jwt
- Refresh: same token URL, grant_type=refresh_token, DPoP, optional x-agent-user-account
- After callback, official CLI redirects to {portal}/login?login_succeed=true|false&locale=
"""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunparse

import httpx

from codearts2api.oauth.dpop import DPoPKeyPair, build_dpop_proof, generate_p256_keypair
from codearts2api.oauth.pkce import PkcePair, generate_pkce_pair, generate_ticket_id
from codearts2api.oauth.session import OAuthSession, SessionStore

logger = logging.getLogger(__name__)

DEFAULT_PORTAL_URL = "https://codearts.huaweicloud.com/portal"
DEFAULT_STS_URL = "https://sts.cn-north-4.myhuaweicloud.com"
DEFAULT_SNAP_MANAGER_URL = "https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager"
DEFAULT_CLIENT_ID = "CodeArts_Tui"
DEFAULT_LOCALE = "zh-cn"
CALLBACK_PATH = "/oauth/callback"
TOKEN_PATH = "/v1/oauth2/tokens"
TICKET_PATH = "/v1/login/ticket"
CALLBACK_TIMEOUT_SECONDS = 300
TICKET_ATTEMPTS = 10
TICKET_INTERVAL_SECONDS = 1.0
# A manually pasted callback is validated, never fetched. Cap it so a pasted
# blob cannot be buffered, logged, or forwarded to the token endpoint.
MAX_MANUAL_CALLBACK_LENGTH = 4096
MAX_CALLBACK_VALUE_LENGTH = 4096
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class OAuthEndpoints:
    portal_url: str = DEFAULT_PORTAL_URL
    sts_url: str = DEFAULT_STS_URL
    snap_manager_url: str = DEFAULT_SNAP_MANAGER_URL
    client_id: str = DEFAULT_CLIENT_ID
    locale: str = DEFAULT_LOCALE

    @property
    def token_url(self) -> str:
        return self.sts_url.rstrip("/") + TOKEN_PATH


def build_authorize_url(
    endpoints: OAuthEndpoints,
    *,
    port: int,
    code_challenge: str,
    ticket_id: str,
    code_challenge_method: str = "SHA-256",
) -> str:
    """Build the official CLI authorize URL. Query names match the official client."""
    query = urlencode(
        {
            "locale": endpoints.locale,
            "client_id": endpoints.client_id,
            "port": str(port),
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "ticket_id": ticket_id,
        }
    )
    return f"{endpoints.portal_url.rstrip('/')}/authorize?{query}"


def success_redirect_url(endpoints: OAuthEndpoints, ok: bool) -> str:
    flag = "true" if ok else "false"
    return f"{endpoints.portal_url.rstrip('/')}/login?login_succeed={flag}&locale={endpoints.locale}"


def map_token_response(payload: dict[str, Any], *, client_id: str, dpop: DPoPKeyPair) -> OAuthSession:
    credentials = payload.get("credentials")
    if not isinstance(credentials, dict):
        credentials = payload
    expires = credentials.get("expiration") or payload.get("expires_at")
    session = OAuthSession(
        access_key_id=str(credentials.get("access_key_id") or credentials.get("access") or ""),
        secret_access_key=str(credentials.get("secret_access_key") or credentials.get("secret") or ""),
        security_token=credentials.get("security_token") or credentials.get("securitytoken"),
        refresh_token=payload.get("refresh_token") or payload.get("refreshToken") or "",
        expires_at=_coerce_expires(expires),
        client_id=client_id,
        dpop=dpop,
        source="oauth",
    )
    if not session.access_key_id or not session.secret_access_key:
        raise ValueError("Token response is missing access_key_id / secret_access_key")
    return session


def map_ticket_response(payload: dict[str, Any], *, client_id: str) -> OAuthSession:
    """Map the official CLI ticket poll body (credential.access / secret / securitytoken / expires_at)."""
    credential = payload.get("credential") or payload.get("credentials") or payload
    if not isinstance(credential, dict):
        raise ValueError("Ticket response is missing credential object")
    session = OAuthSession(
        access_key_id=str(credential.get("access") or credential.get("access_key_id") or ""),
        secret_access_key=str(credential.get("secret") or credential.get("secret_access_key") or ""),
        security_token=credential.get("securitytoken") or credential.get("security_token"),
        refresh_token=str(payload.get("refresh_token") or payload.get("refreshToken") or ""),
        expires_at=_coerce_expires(credential.get("expires_at") or credential.get("expiration")),
        client_id=client_id,
        dpop=None,
        source="oauth-ticket",
    )
    if not session.access_key_id or not session.secret_access_key:
        raise ValueError("Ticket response is missing access / secret")
    return session


def _coerce_expires(value: Any) -> datetime | None:
    from codearts2api.oauth.session import _parse_expires_at

    return _parse_expires_at(value)


def parse_callback_query(query: dict[str, list[str]] | str) -> dict[str, str | None]:
    if isinstance(query, str):
        parsed = parse_qs(query, keep_blank_values=True)
    else:
        parsed = query
    def first(name: str) -> str | None:
        values = parsed.get(name)
        if not values:
            return None
        return values[0]

    return {
        "code": first("code"),
        "secret": first("secret"),
        "redirect": first("redirect"),
    }


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _check_carrier_value(value: str) -> None:
    if not value.strip() or len(value) > MAX_CALLBACK_VALUE_LENGTH or _has_control_chars(value):
        raise ValueError("Invalid callback parameters")


def normalize_manual_callback(raw: object, *, expected_port: int) -> dict[str, str]:
    """Validate a browser-pasted loopback callback URL and return its carrier.

    The URL is never fetched and never forwarded. Only an HTTP loopback host on
    this attempt's callback port and path is accepted, with exactly one nonblank
    ``code`` or ``secret`` query parameter. ``redirect`` may accompany ``code``
    (the automatic path tolerates it) but is always discarded, and unknown query
    keys are ignored rather than rejected so a provider adding metadata does not
    force a retry. Raises ValueError for anything else so callers can answer
    with a static message instead of echoing the input.
    """
    if not isinstance(raw, str):
        raise ValueError("Invalid callback URL")
    text = raw.strip()
    if not text or len(text) > MAX_MANUAL_CALLBACK_LENGTH or _has_control_chars(text) or "#" in text:
        raise ValueError("Invalid callback URL")
    parts = urlsplit(text)
    if parts.scheme.lower() != "http" or not parts.netloc:
        raise ValueError("Invalid callback URL")
    if "@" in parts.netloc:
        raise ValueError("Invalid callback URL")
    host = parts.hostname
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        raise ValueError("Invalid callback URL")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid callback URL") from exc
    if port != expected_port or parts.path != CALLBACK_PATH:
        raise ValueError("Invalid callback URL")

    parsed = parse_qs(parts.query, keep_blank_values=True)
    carriers = [name for name in ("code", "secret") if name in parsed]
    if len(carriers) != 1:
        # Neither carrier, or both at once: ambiguous, so require a retry.
        raise ValueError("Invalid callback URL")
    name = carriers[0]
    if len(parsed[name]) != 1:
        raise ValueError("Invalid callback URL")
    value = parsed[name][0]
    _check_carrier_value(value)
    if name == "secret":
        redirect = parsed.get("redirect")
        if redirect is not None and (
            len(redirect) != 1 or len(redirect[0]) > MAX_CALLBACK_VALUE_LENGTH
        ):
            raise ValueError("Invalid callback URL")
    # The pasted redirect target is dropped: manual submission must never follow it.
    return {name: value}


class LoginError(RuntimeError):
    pass


class CallbackServer:
    """Loopback HTTP server used by the official client: 127.0.0.1 + /oauth/callback."""

    def __init__(self, endpoints: OAuthEndpoints, port: int = 0) -> None:
        self.endpoints = endpoints
        self._requested_port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.pkce: PkcePair | None = None
        self.ticket_id: str | None = None
        self.dpop: DPoPKeyPair | None = None
        self._result: OAuthSession | None = None
        self._error: BaseException | None = None
        self._done = threading.Event()
        self._claim_lock = threading.Lock()
        self._claimed = False
        self._redirect_uri: str | None = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("Callback server is not running")
        return int(self._httpd.server_address[1])

    @property
    def callback_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def start(self, pkce: PkcePair, ticket_id: str, dpop: DPoPKeyPair) -> None:
        self.pkce = pkce
        self.ticket_id = ticket_id
        self.dpop = dpop
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
                # Never log self.path: its query carries code/secret values.
                logger.debug("oauth callback request handled")

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self._send(404, b"Not found")
                    return
                params = parse_callback_query(parsed.query)
                owner._handle(self, params)

            def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._requested_port), Handler)
        # Freeze redirect_uri while the listener exists: the exchange may outlive
        # it, and the token endpoint must see the same URI used for authorize.
        self._redirect_uri = self.callback_url
        # Short poll interval: stop() is called from ASGI handlers, so the
        # default 0.5s serve_forever poll would stall the event loop per request.
        self._thread = threading.Thread(
            target=self._serve,
            name="oauth-callback",
            daemon=True,
        )
        self._thread.start()
        logger.info("OAuth callback server listening on %s", self.callback_url)

    def _serve(self) -> None:
        assert self._httpd is not None
        try:
            self._httpd.serve_forever(poll_interval=0.02)
        except Exception:  # noqa: BLE001
            logger.exception("OAuth callback server stopped unexpectedly")

    def stop(self) -> None:
        # shutdown() returns within the serve_forever poll interval (20ms), and an
        # in-flight exchange owns its own HTTP client, so stopping the listener
        # never truncates a running exchange. Never block the caller for long.
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    @property
    def result(self) -> OAuthSession | None:
        return self._result

    @property
    def error(self) -> BaseException | None:
        return self._error

    def wait(self, timeout: float = CALLBACK_TIMEOUT_SECONDS) -> OAuthSession:
        if not self._done.wait(timeout):
            raise LoginError("OAuth callback timed out; open the authorize URL and complete login in the browser")
        if self._error is not None:
            raise LoginError(str(self._error)) from self._error
        if self._result is None:
            raise LoginError("OAuth login finished without credentials")
        return self._result

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def _claim(self) -> bool:
        """One-shot gate: the first auto/manual callback owns the exchange."""
        with self._claim_lock:
            if self._claimed:
                return False
            self._claimed = True
            return True

    def submit_manual(self, carrier: dict[str, str]) -> None:
        """Start the exchange for a validated pasted carrier.

        The flow is claimed exactly once, so a manual submission races neither a
        browser callback nor a second submission. Returns after starting the
        background exchange; callers poll ``done`` / ``wait``.
        """
        if not self._claim():
            raise ValueError("This authorization attempt already received a callback")
        code = carrier.get("code")
        secret = carrier.get("secret")
        if code:
            target = self._exchange_code
            args: tuple[str, ...] = (code,)
        elif secret:
            target = self._poll_ticket
            args = (secret,)
        else:
            self._finish_error(LoginError("Invalid callback parameters"))
            return
        threading.Thread(target=target, args=args, name="oauth-manual", daemon=True).start()

    def _handle(self, handler: BaseHTTPRequestHandler, params: dict[str, str | None]) -> None:
        code, secret, redirect = params["code"], params["secret"], params["redirect"]
        if not code and not secret:
            handler._send(200, b"OK")  # type: ignore[attr-defined]
            return

        if not self._claim():
            # A duplicate callback (or a manual submission already won) answers
            # with the last known result instead of exchanging twice.
            location = redirect or success_redirect_url(self.endpoints, self._succeeded())
            handler._send(307, b"", {"Location": location})  # type: ignore[attr-defined]
            return

        # Automatic/CLI precedence is unchanged: secret+redirect wins if present.
        if secret and redirect:
            handler._send(307, b"", {"Location": redirect})  # type: ignore[attr-defined]
            threading.Thread(
                target=self._poll_ticket,
                args=(secret,),
                name="oauth-ticket",
                daemon=True,
            ).start()
            return
        if code:
            self._exchange_code(code)
        else:
            self._finish_error(LoginError("Invalid callback parameters"))

        location = redirect or success_redirect_url(self.endpoints, self._succeeded())
        handler._send(307, b"", {"Location": location})  # type: ignore[attr-defined]

    def _succeeded(self) -> bool:
        return self._done.is_set() and self._error is None and self._result is not None

    def _exchange_code(self, code: str) -> None:
        try:
            if not self.pkce or not self.dpop or not self._redirect_uri:
                raise LoginError("PKCE / DPoP state is missing")
            with httpx.Client(timeout=30.0, follow_redirects=False) as http:
                self._result = exchange_authorization_code(
                    self.endpoints,
                    code=code,
                    code_verifier=self.pkce.code_verifier,
                    redirect_uri=self._redirect_uri,
                    dpop=self.dpop,
                    http=http,
                )
            self._done.set()
        except BaseException as exc:  # noqa: BLE001
            self._finish_error(exc)
            # Never log the carrier or upstream error text: it can echo the code.
            logger.error("OAuth code exchange failed (%s)", type(exc).__name__)

    def _finish_error(self, exc: BaseException) -> None:
        self._error = exc
        self._done.set()

    def _poll_ticket(self, secret: str) -> None:
        try:
            if not self.ticket_id:
                raise LoginError("ticket_id is missing")
            with httpx.Client(timeout=30.0, follow_redirects=False) as http:
                self._result = poll_login_ticket(
                    self.endpoints,
                    ticket_id=self.ticket_id,
                    secret=secret,
                    http=http,
                )
        except BaseException as exc:  # noqa: BLE001
            self._finish_error(exc)
            logger.error("Ticket exchange failed (%s)", type(exc).__name__)
        else:
            self._done.set()


def exchange_authorization_code(
    endpoints: OAuthEndpoints,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    dpop: DPoPKeyPair,
    http: httpx.Client,
) -> OAuthSession:
    token_url = endpoints.token_url
    proof = build_dpop_proof(dpop, "POST", token_url)
    body = urlencode(
        {
            "client_id": endpoints.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )
    response = http.post(
        token_url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "DPoP": proof,
        },
        content=body.encode("utf-8"),
    )
    payload = _json_or_error(response, "Token exchange")
    return map_token_response(payload, client_id=endpoints.client_id, dpop=dpop)


def refresh_oauth_session(
    session: OAuthSession,
    endpoints: OAuthEndpoints,
    *,
    http: httpx.Client | None = None,
) -> OAuthSession:
    if not session.can_refresh():
        raise LoginError("Session has no refresh_token or DPoP key pair")
    assert session.dpop is not None
    assert session.refresh_token is not None
    close = False
    if http is None:
        http = httpx.Client(timeout=30.0)
        close = True
    try:
        token_url = endpoints.token_url
        proof = build_dpop_proof(session.dpop, "POST", token_url)
        body = urlencode(
            {
                "client_id": session.client_id or endpoints.client_id,
                "refresh_token": session.refresh_token,
                "grant_type": "refresh_token",
            }
        )
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "DPoP": proof,
            "x-agent-user-account": session.account_id or "",
        }
        response = http.post(token_url, headers=headers, content=body.encode("utf-8"))
        payload = _json_or_error(response, "Token refresh")
        refreshed = map_token_response(payload, client_id=session.client_id, dpop=session.dpop)
        refreshed.account_id = session.account_id
        return refreshed
    finally:
        if close:
            http.close()


def poll_login_ticket(
    endpoints: OAuthEndpoints,
    *,
    ticket_id: str,
    secret: str,
    http: httpx.Client,
    attempts: int = TICKET_ATTEMPTS,
    interval: float = TICKET_INTERVAL_SECONDS,
) -> OAuthSession:
    query = urlencode({"ticket_id": ticket_id, "secret": secret})
    url = f"{endpoints.snap_manager_url.rstrip('/')}{TICKET_PATH}?{query}"
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        time.sleep(interval)
        logger.info("Polling login ticket (%s/%s)", attempt, attempts)
        response = http.get(url, headers={"Accept": "application/json"})
        if response.status_code >= 400:
            last_error = f"HTTP {response.status_code}"
            continue
        try:
            payload = response.json()
        except ValueError:
            last_error = "non-JSON ticket response"
            continue
        if not isinstance(payload, dict):
            last_error = "unexpected ticket payload"
            continue
        try:
            return map_ticket_response(payload, client_id=endpoints.client_id)
        except ValueError as exc:
            last_error = str(exc)
            continue
    raise LoginError(f"Secret exchange failed after {attempts} attempts ({last_error})")


def _json_or_error(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    if response.status_code >= 400 or not isinstance(payload, dict):
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error_description") or payload.get("error") or payload)
        raise LoginError(f"{what} failed: HTTP {response.status_code} {detail}".strip())
    return payload


def run_login(
    endpoints: OAuthEndpoints,
    store: SessionStore,
    *,
    callback_port: int = 0,
    open_browser: bool = True,
    timeout: float = CALLBACK_TIMEOUT_SECONDS,
) -> tuple[str, OAuthSession]:
    """Start the loopback callback, print/open the authorize URL, wait, persist the session."""
    pkce = generate_pkce_pair()
    ticket_id = generate_ticket_id()
    dpop = generate_p256_keypair()
    server = CallbackServer(endpoints, port=callback_port)
    server.start(pkce, ticket_id, dpop)
    try:
        authorize_url = build_authorize_url(
            endpoints,
            port=server.port,
            code_challenge=pkce.code_challenge,
            ticket_id=ticket_id,
            code_challenge_method=pkce.code_challenge_method,
        )
        logger.info("Authorize URL is ready (callback on 127.0.0.1:%s)", server.port)
        if open_browser:
            try:
                webbrowser.open(authorize_url, new=1)
            except Exception:  # noqa: BLE001
                logger.warning("Could not open a local browser; open the printed URL yourself")
        session = server.wait(timeout=timeout)
        store.save(session)
        return authorize_url, session
    finally:
        server.stop()
