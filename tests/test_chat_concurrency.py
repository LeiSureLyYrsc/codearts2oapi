"""Process-local chat concurrency accounting and selective cancellation."""

from __future__ import annotations

import asyncio
import json

import pytest

from codearts2api.chat_concurrency import ChatConcurrency, ChatConcurrencyMiddleware


@pytest.mark.asyncio
async def test_counts_track_until_request_finishes_and_cancel_is_selective() -> None:
    registry = ChatConcurrency()
    entered = {"a": asyncio.Event(), "b": asyncio.Event()}
    finished = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def endpoint(scope, receive, send):
        ticket = scope["state"]["chat_request"]
        name = scope["state"]["account"]
        assert ticket.admit(2)
        assert ticket.bind(name, 1)
        entered[name].set()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"", "more_body": True})
        try:
            await asyncio.Event().wait()
        finally:
            finished[name].set()

    app = ChatConcurrencyMiddleware(endpoint, registry)

    async def request(name):
        sent = []
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            sent.append(message)
        await app({"type": "http", "method": "POST", "path": "/v1/chat/completions", "state": {"account": name}}, receive, send)
        return sent

    # Use two exact endpoint requests; account identity comes from the test request state path.
    async def endpoint(scope, receive, send):
        ticket = scope["state"]["chat_request"]
        name = scope["state"]["account"]
        assert ticket.admit(2)
        assert ticket.bind(name, 1)
        entered[name].set()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        try:
            await asyncio.Event().wait()
        finally:
            finished[name].set()

    app = ChatConcurrencyMiddleware(endpoint, registry)
    a = asyncio.create_task(request("a"))
    b = asyncio.create_task(request("b"))
    await asyncio.gather(entered["a"].wait(), entered["b"].wait())
    assert registry.active_count() == 2
    assert registry.active_count("a") == registry.active_count("b") == 1

    assert registry.cancel_account("a") == 1
    await asyncio.wait_for(finished["a"].wait(), 1)
    assert registry.cancel_account("a") == 0
    assert registry.active_count() == 1
    assert registry.active_count("a") == 0
    assert registry.active_count("b") == 1
    b.cancel()
    await asyncio.gather(a, b, return_exceptions=True)
    assert registry.active_count() == 0
    assert registry.active_count("b") == 0


@pytest.mark.asyncio
async def test_unbind_releases_only_account_slot_and_limits_are_atomic() -> None:
    registry = ChatConcurrency()
    tickets = []

    async def endpoint(scope, receive, send):
        ticket = scope["state"]["chat_request"]
        tickets.append(ticket)
        assert ticket.admit(1)
        assert ticket.bind("account", 1)
        ticket.unbind()
        assert ticket.bind("account", 1)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    app = ChatConcurrencyMiddleware(endpoint, registry)
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        pass
    await app({"type": "http", "method": "POST", "path": "/mount/v1/chat/completions", "root_path": "/mount", "state": {}}, receive, send)
    assert registry.active_count() == registry.active_count("account") == 0
    assert not tickets[0].bind("account", 1)
    tickets[0].unbind()
    assert registry.active_count("account") == 0


@pytest.mark.asyncio
async def test_cancel_before_response_is_json_409() -> None:
    registry = ChatConcurrency()
    entered = asyncio.Event()
    async def endpoint(scope, receive, send):
        ticket = scope["state"]["chat_request"]
        assert ticket.admit(0)
        assert ticket.bind("target", 0)
        entered.set()
        await asyncio.Event().wait()
    messages = []
    async def receive():
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
    app = ChatConcurrencyMiddleware(endpoint, registry)
    task = asyncio.create_task(app({"type": "http", "method": "POST", "path": "/v1/chat/completions", "state": {}}, receive, send))
    await entered.wait()
    assert registry.cancel_account("target") == 1
    await task
    assert messages[0]["status"] == 409
    assert json.loads(messages[1]["body"])["error"]["code"] == 409
    assert registry.active_count() == registry.active_count("target") == 0
