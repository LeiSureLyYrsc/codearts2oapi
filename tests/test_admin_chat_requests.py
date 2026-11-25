"""Verify request limits and cancellation through the real gateway routes."""

import asyncio
import json

import httpx
import pytest

from codearts2api.app import create_app
from codearts2api.client import SnapAccessClient

AUTH = {"Authorization": "Bearer test-local-key"}
CHAT = "/v1/chat/completions"


@pytest.mark.parametrize("limit_kind", ["global", "account"])
async def test_concurrency_limit_and_release_close_stream(settings, limit_kind):
    app = create_app(settings)
    waiting_for_more = asyncio.Event()
    closed = asyncio.Event()
    calls = 0

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            waiting_for_more.set()
            await asyncio.Event().wait()

        async def aclose(self):
            # A genuinely async close must finish before credit is returned.
            await asyncio.sleep(0)
            closed.set()

    async def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=BlockingStream())

    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_http:
        runtime = app.state.runtime
        runtime.client = SnapAccessClient(settings, upstream_http, runtime.resolver)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as http:
            before = (await http.get("/admin/accounts", headers=AUTH)).json()
            [account] = before["accounts"]
            account_url = f"/admin/accounts/{account['id']}"
            if limit_kind == "global":
                result = await http.put("/admin/config", headers=AUTH, json={"chat_concurrency_limit": 1})
            else:
                result = await http.patch(account_url, headers=AUTH, json={"chat_concurrency_limit": 1})
            assert result.status_code == 200, result.text
            assert result.json()["chat_concurrency_limit"] == 1

            body = {"model": "M", "stream": True, "messages": [{"role": "user", "content": "hello"}]}
            stream = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body))
            try:
                await asyncio.wait_for(waiting_for_more.wait(), timeout=3)
                active = (await http.get("/admin/accounts", headers=AUTH)).json()
                assert active["active_chat_requests"] == active["accounts"][0]["active_chat_requests"] == 1
                blocked = await http.post(CHAT, headers=AUTH, json={**body, "stream": False})
                assert blocked.status_code == 429, blocked.text
                assert calls == 1

                release_url = account_url + "/release-requests"
                assert (await http.post(release_url)).status_code == 401
                release = await http.post(release_url, headers=AUTH)
                assert release.json() == {"account_id": account["id"], "cancelled": 1}
                response = await asyncio.wait_for(stream, timeout=3)
                assert response.status_code == 200
                frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                assert frames[0]["choices"][0]["delta"]["content"] == "partial"
                assert frames[-1]["error"]["code"] == 409
                assert closed.is_set()
                final = (await http.get("/admin/accounts", headers=AUTH)).json()
                assert final["active_chat_requests"] == final["accounts"][0]["active_chat_requests"] == 0
                assert (await http.post(release_url, headers=AUTH)).json()["cancelled"] == 0
            finally:
                if not stream.done():
                    stream.cancel()
                    await asyncio.gather(stream, return_exceptions=True)


async def test_global_limit_is_shared_between_accounts(settings):
    app = create_app(settings)
    attempted = asyncio.Event()
    keys = []

    async def upstream(request):
        key = request.headers["authorization"].split("Access=", 1)[1].split(",", 1)[0]
        keys.append(key)
        if len(keys) == 1:
            attempted.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_http:
        runtime = app.state.runtime
        runtime.client = SnapAccessClient(settings, upstream_http, runtime.resolver)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as http:
            configured = await http.put("/admin/config", headers=AUTH, json={"chat_concurrency_limit": 1})
            assert configured.status_code == 200
            extra = await http.post("/admin/accounts", headers=AUTH, json={
                "kind": "aksk", "name": "another-account", "ak": "other-ak", "sk": "other-sk",
            })
            assert extra.status_code == 200, extra.text
            first = asyncio.create_task(http.post(CHAT, headers=AUTH, json={"model": "M"}))
            try:
                await asyncio.wait_for(attempted.wait(), timeout=3)
                status = (await http.get("/admin/accounts", headers=AUTH)).json()
                active = [account for account in status["accounts"] if account["active_chat_requests"]]
                assert len(status["accounts"]) == 2 and len(active) == 1
                assert all(account["chat_concurrency_limit"] == 0 for account in status["accounts"])
                assert (await http.post(CHAT, headers=AUTH, json={"model": "M"})).status_code == 429
                assert len(keys) == 1
                release = await http.post(f"/admin/accounts/{active[0]['id']}/release-requests", headers=AUTH)
                assert release.json()["cancelled"] == 1
                assert (await asyncio.wait_for(first, timeout=3)).status_code == 409
                assert (await http.post(CHAT, headers=AUTH, json={"model": "M"})).status_code == 200
                assert len(keys) == 2 and keys[0] != keys[1]
            finally:
                if not first.done():
                    first.cancel()
                    await asyncio.gather(first, return_exceptions=True)


async def test_release_while_waiting_for_nonstream_upstream_returns_409(settings):
    app = create_app(settings)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def upstream(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_http:
        runtime = app.state.runtime
        runtime.client = SnapAccessClient(settings, upstream_http, runtime.resolver)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as http:
            request = asyncio.create_task(http.post(CHAT, headers=AUTH, json={"model": "M"}))
            try:
                await asyncio.wait_for(started.wait(), timeout=3)
                [account] = (await http.get("/admin/accounts", headers=AUTH)).json()["accounts"]
                cancel = await http.post(f"/admin/accounts/{account['id']}/release-requests", headers=AUTH)
                assert cancel.json()["cancelled"] == 1
                response = await asyncio.wait_for(request, timeout=3)
                assert response.status_code == 409
                assert response.json()["error"]["type"] == "request_cancelled"
                assert stopped.is_set()
                assert runtime.chat_concurrency.active_count() == 0
            finally:
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
