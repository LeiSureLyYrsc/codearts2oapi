"""Chat upstream headers, error decoding, and failover behavior."""

from __future__ import annotations

import httpx
import pytest

from codearts2api.client import SnapAccessClient, UpstreamError
from codearts2api.credentials import ResolvedCredential
from codearts2api.operations import open_chat_stream_with_failover, post_chat_with_failover


class _Resolver:
    def __init__(self, credential: ResolvedCredential) -> None:
        self.credential = credential

    def resolve_account(self, _account):
        return self.credential


class _Pool:
    def __init__(self) -> None:
        self.accounts = [object(), object()]
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
