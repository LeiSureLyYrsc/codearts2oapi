"""Queueing, limits and cancellation for chat requests, through the real gateway.

An account slot stays occupied until the upstream response is actually closed,
so a request over an account limit must wait instead of failing with 429: it may
not touch upstream while parked, it is admitted in arrival order, it prefers a
free candidate account, it leaves the queue on disconnect, and a raised account
limit hands its new capacity to the waiter.

A full global cap is different: the process-wide ceiling cannot be honoured by
waiting, so a newcomer is rejected at once with an OpenAI 429. Requests accepted
earlier keep running, and ones already parked for an account keep their place
even if the cap is later lowered under the current load.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from codearts2api import chat_concurrency as cc
from codearts2api.app import create_app
from codearts2api.client import SnapAccessClient

AUTH = {"Authorization": "Bearer test-local-key"}
CHAT = "/v1/chat/completions"
WAIT = 5


def body(content: str = "ok", *, stream: bool = False) -> dict:
    return {"model": "M", "stream": stream, "messages": [{"role": "user", "content": content}]}


def content_of(response: httpx.Response) -> str:
    return response.json()["choices"][0]["message"]["content"]


class BlockingStream(httpx.AsyncByteStream):
    """An upstream SSE body that stays open until the gateway closes it."""

    def __init__(self) -> None:
        self.consumed = asyncio.Event()  # first frame already handed downstream
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
        self.consumed.set()
        await asyncio.Event().wait()

    async def aclose(self):
        await asyncio.sleep(0)  # a genuinely async close must settle first
        self.closed.set()


class Upstream:
    """Mock upstream: the first call parks, later calls echo the request content.

    ``hold=True`` parks it on an open SSE body; ``hold=False`` parks it on
    ``gate`` and then answers. ``reached_before_close`` records whether a later
    call began while the first SSE body was still open.
    """

    def __init__(self, *, hold: bool = True) -> None:
        self.hold = hold
        self.calls: list[httpx.Request] = []
        self.streams: list[BlockingStream] = []
        self.entered = asyncio.Event()  # first call reached upstream
        self.gate = asyncio.Event()     # releases a held first call
        self.stopped = asyncio.Event()  # a held first call was cancelled
        self.reached_before_close: bool | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if len(self.calls) > 1:
            self.reached_before_close = bool(self.streams) and not self.streams[0].closed.is_set()
            return self.reply(request)
        self.entered.set()
        if self.hold:
            self.streams.append(stream := BlockingStream())
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        try:
            await self.gate.wait()
        finally:
            self.stopped.set()
        return self.reply(request)

    @staticmethod
    def reply(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})

    @property
    def tags(self) -> list[str]:
        """Request tags in the order the gateway actually reached upstream."""
        return [json.loads(r.content)["messages"][0]["content"] for r in self.calls]

    @property
    def keys(self) -> list[str]:
        return [r.headers["authorization"].split("Access=", 1)[1].split(",", 1)[0] for r in self.calls]

    async def hold_open(self) -> BlockingStream:
        """Wait until the first SSE frame has been handed downstream."""
        await asyncio.wait_for(self.entered.wait(), timeout=WAIT)
        await asyncio.wait_for(self.streams[0].consumed.wait(), timeout=WAIT)
        return self.streams[0]


@asynccontextmanager
async def gateway(settings, upstream: Upstream):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_http:
        runtime = app.state.runtime
        runtime.client = SnapAccessClient(settings, upstream_http, runtime.resolver)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as http:
            yield runtime, http, app


async def account_of(http: httpx.AsyncClient) -> dict:
    [account] = (await http.get("/admin/accounts", headers=AUTH)).json()["accounts"]
    return account


async def add_account(http: httpx.AsyncClient) -> None:
    result = await http.post("/admin/accounts", headers=AUTH, json={
        "kind": "aksk", "name": "another-account", "ak": "other-ak", "sk": "other-sk",
    })
    assert result.status_code == 200, result.text


async def set_limit(http: httpx.AsyncClient, kind: str, account: dict, value: int) -> None:
    if kind == "global":
        result = await http.put("/admin/config", headers=AUTH, json={"chat_concurrency_limit": value})
    else:
        result = await http.patch(f"/admin/accounts/{account['id']}", headers=AUTH,
                                  json={"chat_concurrency_limit": value})
    assert result.status_code == 200, result.text
    assert result.json()["chat_concurrency_limit"] == value


async def release(http: httpx.AsyncClient, account: dict) -> dict:
    return (await http.post(f"/admin/accounts/{account['id']}/release-requests", headers=AUTH)).json()


async def queued(runtime, count: int, *tasks: asyncio.Task) -> None:
    """Wait until ``count`` requests are parked; fail if one is answered early."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while (waiting := runtime.chat_concurrency.waiting_count()) != count:
        if any(task.done() for task in tasks):
            pytest.fail(f"expected {count} queued, saw {waiting}: a request was answered instead")
        if loop.time() > deadline:
            pytest.fail(f"expected {count} queued, saw {waiting}")
        await asyncio.sleep(0)


async def cancel(*tasks) -> None:
    pending = [task for task in tasks if task is not None]
    for task in pending:
        if not task.done():
            task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


SCOPE = {
    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
    "method": "POST", "scheme": "http", "path": CHAT, "raw_path": CHAT.encode(),
    "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000),
    "server": ("fixture", 80),
    "headers": [(b"authorization", b"Bearer test-local-key"),
                (b"content-type", b"application/json")],
}


@asynccontextmanager
async def occupied(settings, *, stream: bool = True):
    """One account limited to 1, with a request already holding the slot."""
    upstream = Upstream(hold=stream)
    async with gateway(settings, upstream) as (runtime, http, app):
        account = await account_of(http)
        await set_limit(http, "account", account, 1)
        first = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body(stream=stream)))
        try:
            if stream:
                await upstream.hold_open()
            else:
                await asyncio.wait_for(upstream.entered.wait(), timeout=WAIT)
            yield runtime, http, app, account, upstream, first
        finally:
            upstream.gate.set()
            if not first.done():
                await release(http, account)
            await cancel(first)


async def test_over_limit_request_queues_and_release_closes_stream(settings):
    async with occupied(settings) as (runtime, http, _, account, upstream, stream):
        counts = (await http.get("/admin/accounts", headers=AUTH)).json()
        assert counts["active_chat_requests"] == counts["accounts"][0]["active_chat_requests"] == 1

        waiter = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body()))
        try:
            await queued(runtime, 1, waiter)
            assert len(upstream.calls) == 1, "a queued request must not contact upstream"
            assert (await http.get("/admin/accounts", headers=AUTH)).json()["active_chat_requests"] == 1

            assert (await http.post(f"/admin/accounts/{account['id']}/release-requests")).status_code == 401
            assert await release(http, account) == {"account_id": account["id"], "cancelled": 1}
            response = await asyncio.wait_for(stream, timeout=WAIT)
            frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert response.status_code == 200
            assert frames[0]["choices"][0]["delta"]["content"] == "partial"
            assert frames[-1]["error"]["code"] == 409
            assert upstream.streams[0].closed.is_set()

            admitted = await asyncio.wait_for(waiter, timeout=WAIT)
            assert admitted.status_code == 200, admitted.text
            assert content_of(admitted) == "ok"
            assert upstream.reached_before_close is False, "slot was granted before the stream closed"
            final = (await http.get("/admin/accounts", headers=AUTH)).json()
            assert final["active_chat_requests"] == final["accounts"][0]["active_chat_requests"] == 0
            assert runtime.chat_concurrency.waiting_count() == 0
            assert (await release(http, account))["cancelled"] == 0
        finally:
            await cancel(waiter)


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
async def test_global_cap_rejects_new_arrival_with_429(settings, stream):
    """A newcomer is refused at once when the process-wide cap is full.

    The cap spans accounts, so an idle second account adds nothing: the request
    must not be parked, must not touch upstream, and the request already holding
    the slot must keep running. Both response modes get the same JSON error.
    """
    upstream = Upstream()
    async with gateway(settings, upstream) as (runtime, http, _):
        await set_limit(http, "global", await account_of(http), 1)
        await add_account(http)
        first = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body("first", stream=True)))
        busy = None
        try:
            held = await upstream.hold_open()
            assert runtime.chat_concurrency.active_count() == 1
            status = (await http.get("/admin/accounts", headers=AUTH)).json()
            assert len(status["accounts"]) == 2, "the reject must come from the global cap, not the only account"
            busy = [account for account in status["accounts"] if account["active_chat_requests"]]
            assert len(busy) == 1 and all(account["chat_concurrency_limit"] == 0 for account in status["accounts"])

            rejected = await asyncio.wait_for(
                http.post(CHAT, headers=AUTH, json=body("rejected", stream=stream)), timeout=WAIT)
            assert rejected.status_code == 429, rejected.text
            assert rejected.headers["content-type"].startswith("application/json")
            assert rejected.json()["error"] == {
                "message": "Gateway chat request concurrency limit reached",
                "type": "invalid_request_error",
                "code": "429",
            }
            assert runtime.chat_concurrency.waiting_count() == 0, "the cap must reject, not queue"
            assert len(upstream.calls) == 1, "a rejected request must not contact upstream"
            assert not first.done() and not held.closed.is_set()
            assert (await http.get("/admin/accounts", headers=AUTH)).json()["active_chat_requests"] == 1
        finally:
            if busy and not first.done():
                await release(http, busy[0])
            await cancel(first)


async def test_global_limit_is_shared_between_accounts(settings):
    upstream = Upstream(hold=False)
    async with gateway(settings, upstream) as (runtime, http, _):
        await set_limit(http, "global", await account_of(http), 1)
        await add_account(http)
        first = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body("first")))
        try:
            await asyncio.wait_for(upstream.entered.wait(), timeout=WAIT)
            status = (await http.get("/admin/accounts", headers=AUTH)).json()
            active = [account for account in status["accounts"] if account["active_chat_requests"]]
            assert len(status["accounts"]) == 2 and len(active) == 1
            assert all(account["chat_concurrency_limit"] == 0 for account in status["accounts"])

            rejected = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=body("second")), timeout=WAIT)
            assert rejected.status_code == 429, rejected.text
            assert rejected.json()["error"]["message"] == "Gateway chat request concurrency limit reached"
            assert len(upstream.calls) == 1, "a rejected arrival must not contact upstream"

            assert (await release(http, active[0]))["cancelled"] == 1
            assert (await asyncio.wait_for(first, timeout=WAIT)).status_code == 409

            # After the global slot is returned, the pool serves the next request
            # from the other account: the rejected arrival above must not have
            # disturbed rotation or pinned the pool to the account that just ran.
            started = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=body("third")), timeout=WAIT)
            assert started.status_code == 200, started.text
            assert len(upstream.keys) == 2 and upstream.keys[0] != upstream.keys[1]
        finally:
            upstream.gate.set()
            await cancel(first)


async def test_saturated_account_is_skipped_when_another_candidate_is_free(settings):
    upstream = Upstream()
    async with gateway(settings, upstream) as (runtime, http, _):
        configured = await http.put("/admin/config", headers=AUTH,
                                    json={"chat_concurrency_limit": 0, "pool_strategy": "first_available"})
        assert configured.status_code == 200, configured.text
        await add_account(http)
        for account in (await http.get("/admin/accounts", headers=AUTH)).json()["accounts"]:
            assert (await http.patch(f"/admin/accounts/{account['id']}", headers=AUTH,
                                     json={"chat_concurrency_limit": 1})).status_code == 200

        first = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body(stream=True)))
        busy = None
        try:
            await upstream.hold_open()
            status = (await http.get("/admin/accounts", headers=AUTH)).json()
            busy = [account for account in status["accounts"] if account["active_chat_requests"]]
            assert len(busy) == 1 and status["active_chat_requests"] == 1

            started = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=body()), timeout=WAIT)
            assert started.status_code == 200, started.text
            assert len(upstream.keys) == 2 and upstream.keys[0] != upstream.keys[1]
            assert runtime.chat_concurrency.waiting_count() == 0, "a free candidate must not queue"
            assert not first.done() and not upstream.streams[0].closed.is_set()
        finally:
            if busy is not None and not first.done():
                await release(http, busy[0])
            await cancel(first)


async def test_queued_requests_are_admitted_in_fifo_order(settings):
    async with occupied(settings) as (runtime, http, _, account, upstream, stream):
        waiters: list[asyncio.Task] = []
        try:
            for index in range(1, 4):
                waiters.append(asyncio.create_task(http.post(CHAT, headers=AUTH, json=body(f"w{index}"))))
                await queued(runtime, index, *waiters)
            assert len(upstream.calls) == 1, "no waiter may run before the slot is free"

            assert (await release(http, account))["cancelled"] == 1
            await asyncio.wait_for(stream, timeout=WAIT)
            responses = await asyncio.wait_for(asyncio.gather(*waiters), timeout=WAIT)
            assert [response.status_code for response in responses] == [200, 200, 200]
            # Real admission order, from the calls that actually reached upstream;
            # gather() input order alone would not prove FIFO.
            assert upstream.tags == ["ok", "w1", "w2", "w3"], upstream.tags
            assert [content_of(response) for response in responses] == ["w1", "w2", "w3"]
        finally:
            await cancel(*waiters)


async def test_queue_capacity_rejects_extra_waiter_with_429(settings):
    assert cc.QUEUE_MAX_WAITERS == 128, "the published backlog default must stay 128"
    async with occupied(settings) as (runtime, http, _, _, upstream, _):
        waiters = [asyncio.create_task(http.post(CHAT, headers=AUTH, json=body(f"q{index}")))
                   for index in range(cc.QUEUE_MAX_WAITERS)]
        try:
            await queued(runtime, cc.QUEUE_MAX_WAITERS, *waiters)
            assert len(upstream.calls) == 1, "queued requests must not reach upstream"

            rejected = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=body()), timeout=WAIT)
            assert rejected.status_code == 429, rejected.text
            assert rejected.json()["error"]["code"] == "429"
            assert len(upstream.calls) == 1
            assert runtime.chat_concurrency.waiting_count() == cc.QUEUE_MAX_WAITERS
        finally:
            await cancel(*waiters)


async def test_queue_timeout_returns_429_without_waiting_a_minute(settings, monkeypatch):
    assert cc.QUEUE_WAIT_TIMEOUT_SECONDS == 60.0
    monkeypatch.setattr(cc, "QUEUE_WAIT_TIMEOUT_SECONDS", 0.2)

    async with occupied(settings) as (runtime, http, _, account, upstream, stream):
        waiter = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body()))
        try:
            await queued(runtime, 1, waiter)
            timed_out = await asyncio.wait_for(waiter, timeout=WAIT)
            assert timed_out.status_code == 429, timed_out.text
            assert runtime.chat_concurrency.waiting_count() == 0, "a timed-out waiter must leave the queue"
            assert len(upstream.calls) == 1

            assert (await release(http, account))["cancelled"] == 1
            await asyncio.wait_for(stream, timeout=WAIT)
            assert runtime.chat_concurrency.active_count() == 0
        finally:
            await cancel(waiter)


async def test_queued_request_disconnect_never_starts_upstream(settings):
    async with occupied(settings) as (runtime, http, app, account, upstream, stream):
        # A controllable native ASGI receive: the client vanishes while parked.
        disconnect = asyncio.Event()
        sent: list[dict] = []
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": json.dumps(body("dropped")).encode(),
                        "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        waiter = asyncio.create_task(app({**SCOPE, "state": {}}, receive, send))
        try:
            await queued(runtime, 1, waiter)
            assert len(upstream.calls) == 1

            disconnect.set()
            await asyncio.wait_for(waiter, timeout=WAIT)
            assert len(upstream.calls) == 1, "a disconnected waiter must not start upstream"
            assert runtime.chat_concurrency.waiting_count() == 0
            assert not any(message["type"] == "http.response.start" for message in sent)

            assert (await release(http, account))["cancelled"] == 1
            await asyncio.wait_for(stream, timeout=WAIT)
            assert upstream.streams[0].closed.is_set()

            fresh = await http.post(CHAT, headers=AUTH, json=body())
            assert fresh.status_code == 200, fresh.text
            assert len(upstream.calls) == 2, "the disconnect must not leak credit"
        finally:
            await cancel(waiter)


async def test_finished_request_hands_slot_to_waiter(settings):
    async with occupied(settings, stream=False) as (runtime, http, _, _, upstream, first):
        waiter = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body("second")))
        try:
            await queued(runtime, 1, waiter)
            assert len(upstream.calls) == 1

            upstream.gate.set()  # ordinary completion, no release and no disconnect
            assert (await asyncio.wait_for(first, timeout=WAIT)).status_code == 200
            admitted = await asyncio.wait_for(waiter, timeout=WAIT)
            assert admitted.status_code == 200, admitted.text
            assert content_of(admitted) == "second"
            assert len(upstream.calls) == 2
        finally:
            await cancel(waiter)


async def test_account_saturation_queues_while_global_cap_has_room(settings):
    """The gate fires only at the global cap; spare global capacity still queues."""
    async with occupied(settings) as (runtime, http, _, account, upstream, stream):
        await set_limit(http, "global", account, 2)
        waiter = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body("queued")))
        try:
            await queued(runtime, 1, waiter)
            assert runtime.chat_concurrency.active_count() == 1, "the cap must be read as spare here"
            assert len(upstream.calls) == 1

            assert (await release(http, account))["cancelled"] == 1
            await asyncio.wait_for(stream, timeout=WAIT)
            admitted = await asyncio.wait_for(waiter, timeout=WAIT)
            assert admitted.status_code == 200, admitted.text
            assert content_of(admitted) == "queued"
        finally:
            await cancel(waiter)


async def test_lowered_global_limit_does_not_evict_parked_request(settings):
    """A request parked for an account keeps its place when the cap drops.

    Only a newcomer is refused at the cap; a request already waiting must not be
    evicted, or lowering the limit would strand a client that was admitted
    before the change. Once the active request finishes, the parked one still
    gets the slot the cap now leaves free.
    """
    async with occupied(settings) as (runtime, http, _, account, upstream, stream):
        waiter = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body("parked")))
        try:
            await queued(runtime, 1, waiter)

            # Cap now equals the current load: a newcomer is refused at once.
            await set_limit(http, "global", account, 1)
            # While the account is also saturated, "still queued" alone could come
            # from the account limit rather than the cap. Give the account a free
            # slot first, so only the global cap can keep the parked request out.
            await set_limit(http, "account", account, 2)

            blocked = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=body("blocked")), timeout=WAIT)
            assert blocked.status_code == 429, blocked.text
            assert blocked.json()["error"]["message"] == "Gateway chat request concurrency limit reached"
            # The newcomer round trip gave the woken queue head many turns to
            # re-run reserve(); it must still be parked and have taken no slot.
            assert runtime.chat_concurrency.waiting_count() == 1, "the parked request must stay queued"
            assert len(upstream.calls) == 1, "a full global cap must not admit even the parked request"
            assert runtime.chat_concurrency.active_count() == 1

            assert (await release(http, account))["cancelled"] == 1
            await asyncio.wait_for(stream, timeout=WAIT)
            admitted = await asyncio.wait_for(waiter, timeout=WAIT)
            assert admitted.status_code == 200, admitted.text
            assert content_of(admitted) == "parked"
        finally:
            await cancel(waiter)


async def test_raising_account_limit_wakes_queued_request_without_finishing_first(settings):
    async with occupied(settings) as (runtime, http, _, account, upstream, stream):
        waiter = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body()))
        try:
            await queued(runtime, 1, waiter)
            assert len(upstream.calls) == 1

            await set_limit(http, "account", account, 2)
            admitted = await asyncio.wait_for(waiter, timeout=WAIT)
            assert admitted.status_code == 200, admitted.text
            assert len(upstream.calls) == 2
            assert not stream.done(), "the waiter must not need the first request to finish"
            assert not upstream.streams[0].closed.is_set()
        finally:
            await cancel(waiter)


async def test_queued_request_waits_for_upstream_close_when_send_is_cancelled(settings):
    """A cancelled downstream send must close upstream before the slot is handed over.

    The hang point here is the response wrapper's ``send`` (first SSE body frame),
    not upstream reading, so this covers ``_ChatStreamingResponse``: cancelling a
    response that is suspended mid-send must still close the upstream stream, and
    only then may the queued request start.
    """
    upstream = Upstream()
    async with gateway(settings, upstream) as (runtime, http, app):
        account = await account_of(http)
        await set_limit(http, "account", account, 1)

        frame_reached = asyncio.Event()
        gated = asyncio.Event()
        sent: list[dict] = []
        first_frame_held = False
        body_read = False

        async def receive():
            nonlocal body_read
            if not body_read:
                body_read = True
                return {"type": "http.request", "body": json.dumps(body(stream=True)).encode(),
                        "more_body": False}
            await asyncio.Event().wait()  # admitted at once: no disconnect watcher runs

        async def send(message):
            nonlocal first_frame_held
            if message["type"] == "http.response.body" and message.get("body") and not first_frame_held:
                first_frame_held = True
                frame_reached.set()
                await gated.wait()  # suspended mid-send until the operator cancels
            sent.append(message)

        # ASGI 2.4: StreamingResponse streams directly, so the hang is in send.
        scope = {**SCOPE, "asgi": {"version": "3.0", "spec_version": "2.4"}}
        first = asyncio.create_task(app(scope, receive, send))
        waiter = None
        try:
            await asyncio.wait_for(frame_reached.wait(), timeout=WAIT)
            assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
            assert runtime.chat_concurrency.active_count() == 1

            waiter = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body("second")))
            await queued(runtime, 1, waiter)
            assert len(upstream.calls) == 1, "the queued request must not contact upstream"

            assert (await release(http, account))["cancelled"] == 1
            await asyncio.wait_for(first, timeout=WAIT)
            admitted = await asyncio.wait_for(waiter, timeout=WAIT)
            assert admitted.status_code == 200, admitted.text
            assert content_of(admitted) == "second"

            # The queued request only reached upstream after the first aclose.
            assert upstream.reached_before_close is False
            assert upstream.streams[0].closed.is_set()
            assert any(b"request_cancelled" in message.get("body", b"") for message in sent)
            assert runtime.chat_concurrency.active_count() == runtime.chat_concurrency.waiting_count() == 0
        finally:
            gated.set()
            await cancel(waiter, first)


async def test_release_while_waiting_for_nonstream_upstream_returns_409(settings):
    upstream = Upstream(hold=False)
    async with gateway(settings, upstream) as (runtime, http, _):
        account = await account_of(http)
        request = asyncio.create_task(http.post(CHAT, headers=AUTH, json=body()))
        try:
            await asyncio.wait_for(upstream.entered.wait(), timeout=WAIT)
            assert (await release(http, account))["cancelled"] == 1
            response = await asyncio.wait_for(request, timeout=WAIT)
            assert response.status_code == 409
            assert response.json()["error"]["type"] == "request_cancelled"
            assert upstream.stopped.is_set()
            assert runtime.chat_concurrency.active_count() == 0
        finally:
            upstream.gate.set()
            await cancel(request)
