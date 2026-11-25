"""Chat upstream headers, error decoding, and failover behavior."""

from __future__ import annotations

import httpx
import pytest

from codearts2api.client import SnapAccessClient, UpstreamError, chat_error_status, upstream_error_code
from codearts2api.credentials import ResolvedCredential
from codearts2api.operations import open_chat_stream_with_failover, post_chat_with_failover


class _Resolver:
    def __init__(self, credential: ResolvedCredential) -> None:
        self.credential = credential

    def resolve_account(self, _account):
        return self.credential


class _Pool:
    def __init__(self) -> None:
        self.accounts = [
            type("TestAccount", (), {"id": f"account-{index}", "name": f"Test {index}"})()
            for index in (1, 2)
        ]
        self.cooldowns: list[tuple[str, int]] = []

    def iter_candidates(self):
        return iter(self.accounts)

    def clear_cooldown(self, _account_id):
        pass

    def mark_cooldown(self, account_id, status):
        self.cooldowns.append((account_id, status))


def _credential() -> ResolvedCredential:
    return ResolvedCredential("test-ak", "test-sk", None, "oauth")


@pytest.mark.asyncio
async def test_chat_post_signed_headers_and_huawei_400(settings) -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"error_code": "BAD_REQUEST", "error_msg": "Unsupported model"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SnapAccessClient(settings, http)
    payload = {"model": "模型/a b", "messages": []}
    with pytest.raises(UpstreamError, match="Unsupported model") as error:
        await client.post_chat(payload, credential=_credential())
    request = seen[0]
    assert request.headers["x-language"] == "zh-cn"
    assert request.headers["model-id"] == "%E6%A8%A1%E5%9E%8B%2Fa%20b"
    assert request.headers["model-name"] == request.headers["model-id"]
    assert len(request.headers["x-snap-traceid"]) == 32
    authorization = request.headers["authorization"].lower()
    assert "signedheaders=" in authorization
    assert all(name in authorization for name in ("content-type", "model-id", "model-name", "x-language", "x-snap-traceid"))
    assert error.value.status_code == 400
    await http.aclose()


@pytest.mark.asyncio
async def test_400_is_not_retried_and_nested_string_error_is_safe(settings) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "Invalid request"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SnapAccessClient(settings, http)
    pool = _Pool()
    resolver = _Resolver(_credential())
    with pytest.raises(UpstreamError, match="Invalid request"):
        await post_chat_with_failover(client=client, resolver=resolver, pool=pool, payload={"model": "M"})
    assert calls == 1
    assert pool.cooldowns == []
    await http.aclose()


@pytest.mark.asyncio
async def test_sse_error_envelope_is_decoded_and_response_closed(settings) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            text='data: {"error":{"errorMsg":"Model is unavailable"}}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SnapAccessClient(settings, http)
    with pytest.raises(UpstreamError, match="Model is unavailable") as error:
        await open_chat_stream_with_failover(
            client=client,
            resolver=_Resolver(_credential()),
            pool=_Pool(),
            payload={"model": "M", "stream": True},
        )
    assert error.value.status_code == 400
    await http.aclose()


@pytest.mark.asyncio
async def test_chat_session_id_is_signed_for_post_and_stream(settings) -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("accept") == "text/event-stream":
            return httpx.Response(200, text="data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"ok": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SnapAccessClient(settings, http)
    credential = _credential()
    await client.post_chat({"model": "M"}, credential=credential, session_id="opencode-chat-1")
    stream_cm = client.stream_chat(
        {"model": "M", "stream": True}, credential=credential, session_id="opencode-chat-2"
    )
    async with stream_cm as response:
        assert response.status_code == 200

    assert [request.headers["user-session-id"] for request in seen] == ["opencode-chat-1", "opencode-chat-2"]
    for request in seen:
        authorization = request.headers["authorization"].lower()
        assert "user-session-id" in authorization
        assert "x-snap-traceid" in authorization
    await http.aclose()


@pytest.mark.asyncio
async def test_chat_failover_threads_session_id(settings) -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-session-id"])
        return httpx.Response(200, json={"ok": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SnapAccessClient(settings, http)
    await post_chat_with_failover(
        client=client,
        resolver=_Resolver(_credential()),
        pool=_Pool(),
        payload={"model": "M"},
        session_id="same-session",
    )
    assert seen == ["same-session"]
    await http.aclose()


def test_in_band_error_code_and_status_helpers() -> None:
    inferhub = {"error": {"code": "InferHub.001001005.400", "message": "invalid param"}}
    assert upstream_error_code(inferhub) == "InferHub.001001005.400"
    assert chat_error_status(inferhub, 200) == 400

    concurrency = {"error_msg": "并发会话数已达上限(3个)"}
    assert chat_error_status(concurrency, 200) == 429
    assert chat_error_status({"message": "ordinary business error"}, 200) == 200
    assert upstream_error_code({"errorCode": "bad code !"}) is None


@pytest.mark.asyncio
async def test_session_limit_429_does_not_fail_over(settings) -> None:
    seen = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return httpx.Response(429, json={"error_msg": "并发会话数已达上限(3个)"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool = _Pool()
    with pytest.raises(UpstreamError) as error:
        await post_chat_with_failover(
            client=SnapAccessClient(settings, http),
            resolver=_Resolver(_credential()),
            pool=pool,
            payload={"model": "M"},
        )
    assert error.value.status_code == 429
    assert seen == 1
    assert pool.cooldowns == []
    await http.aclose()
