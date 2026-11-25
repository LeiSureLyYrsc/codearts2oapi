"""Upstream concurrent-session cap: the gateway polls instead of failing the chat.

The provider's explicit cap (HTTP 400/429, an HTTP 200 first SSE event, or a
non-stream JSON body whose message is 并发会话数已达上限) must not reach the
client while it can still lift: the request keeps its global slot, releases
its account slot between polls, backs off per account (an idle alternate
account is tried at once), keeps the upstream session id across attempts, and
finally answers with the recovered output. Ordinary 400s and non-cap 429s
fail exactly once, a client disconnect during backoff stops the poll and
frees the slot, and a cap reported after content was streamed is never
replayed.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from codearts2api import session_limit
from codearts2api.app import create_app
from codearts2api.client import SnapAccessClient

LIMIT = "并发会话数已达上限(3个)，请关闭部分会话后重试。"
SESSION = "ses_retry"
AUTH = {"Authorization": "Bearer test-local-key", "X-Session-Affinity": SESSION}
CHAT = "/v1/chat/completions"
WAIT = 10

SCOPE = {
    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
    "method": "POST", "scheme": "http", "path": CHAT, "raw_path": CHAT.encode(),
    "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000),
    "server": ("fixture", 80),
    "headers": [(b"authorization", b"Bearer test-local-key"),
                (b"x-session-affinity", SESSION.encode()),
                (b"content-type", b"application/json")],
}


@pytest.fixture
def fast_backoff(monkeypatch):
    """Shrink the session-limit backoff so polling tests run in milliseconds."""
    monkeypatch.setattr(session_limit, "SESSION_LIMIT_INITIAL_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(session_limit, "SESSION_LIMIT_MAX_DELAY_SECONDS", 0.02)
    monkeypatch.setattr(session_limit, "SESSION_LIMIT_JITTER_RATIO", 0.0)


@pytest.fixture
def parked_backoff(monkeypatch):
    """A backoff long enough to observe the between-poll window deterministically."""
    monkeypatch.setattr(session_limit, "SESSION_LIMIT_INITIAL_DELAY_SECONDS", 0.3)
    monkeypatch.setattr(session_limit, "SESSION_LIMIT_MAX_DELAY_SECONDS", 0.3)
    monkeypatch.setattr(session_limit, "SESSION_LIMIT_JITTER_RATIO", 0.0)


def chat_body(*, stream: bool = False) -> dict:
    return {"model": "M", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


def content_of(response: httpx.Response) -> str:
    return response.json()["choices"][0]["message"]["content"]


def session_ids(calls: list[httpx.Request]) -> list[str | None]:
    return [call.headers.get("user-session-id") for call in calls]


def access_keys(calls: list[httpx.Request]) -> list[str]:
    return [call.headers["authorization"].split("Access=", 1)[1].split(",", 1)[0] for call in calls]


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def limit_json(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error_msg": LIMIT})


def ok_json(content: str = "recovered") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    })


def ok_sse(content: str = "recovered") -> httpx.Response:
    chunk = {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}]}
    return httpx.Response(200, text=sse(chunk) + "data: [DONE]\n\n",
                          headers={"Content-Type": "text/event-stream"})


def limit_sse() -> httpx.Response:
    return httpx.Response(200, text=": heartbeat\n\n" + sse({"error_msg": LIMIT}),
                          headers={"Content-Type": "text/event-stream"})


@asynccontextmanager
async def gateway(settings, handler):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream_http:
        runtime = app.state.runtime
        runtime.client = SnapAccessClient(settings, upstream_http, runtime.resolver)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as http:
            yield runtime, http, app


async def wait_calls(calls: list, count: int, *tasks: asyncio.Task) -> None:
    """Wait until upstream saw ``count`` calls; fail if a request ends early."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while len(calls) < count:
        if any(task.done() for task in tasks):
            pytest.fail(f"expected {count} upstream calls, saw {len(calls)}: the request finished instead")
        if loop.time() > deadline:
            pytest.fail(f"expected {count} upstream calls, saw {len(calls)}")
        await asyncio.sleep(0.01)


async def wait_account_slot_released(runtime, account_id: str, *tasks: asyncio.Task) -> None:
    """Wait until the polling request parks in backoff and returns its account slot."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while runtime.chat_concurrency.active_count(account_id) != 0:
        if any(task.done() for task in tasks):
            pytest.fail("the request finished before parking in backoff")
        if loop.time() > deadline:
            pytest.fail("the polling request never released its account slot")
        await asyncio.sleep(0.01)


async def settle(task: asyncio.Task, *events: asyncio.Event) -> None:
    for event in events:
        event.set()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("status", [400, 429], ids=["http400", "http429"])
@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
async def test_http_session_limit_error_is_polled_until_it_recovers(settings, fast_backoff, status, stream):
    """The cap as an HTTP 400/429 error is never returned; the poll recovers."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return limit_json(status)
        return ok_sse() if stream else ok_json()

    async with gateway(settings, handler) as (runtime, http, _):
        response = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=chat_body(stream=stream)), timeout=WAIT)
        assert response.status_code == 200, response.text
        assert LIMIT not in response.text
        if stream:
            assert '"content": "recovered"' in response.text
            assert response.text.rstrip().endswith("data: [DONE]")
        else:
            assert content_of(response) == "recovered"
        assert len(calls) == 2
        assert session_ids(calls) == [SESSION, SESSION]
        assert runtime.chat_concurrency.active_count() == 0


async def test_first_sse_session_limit_event_is_polled_until_it_recovers(settings, fast_backoff):
    """An HTTP 200 whose first SSE event is the cap is retried, not returned."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return limit_sse()
        return ok_sse()

    async with gateway(settings, handler) as (runtime, http, _):
        response = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=chat_body(stream=True)), timeout=WAIT)
        assert response.status_code == 200, response.text
        assert '"content": "recovered"' in response.text
        assert LIMIT not in response.text
        assert response.text.rstrip().endswith("data: [DONE]")
        assert len(calls) == 2
        assert session_ids(calls) == [SESSION, SESSION]
        assert runtime.chat_concurrency.active_count() == 0


@pytest.mark.parametrize("key", ["error_msg", "message"], ids=["error_msg", "message"])
async def test_nonstream_json_session_limit_is_polled_until_it_recovers(settings, fast_backoff, key):
    """An HTTP 200 JSON error body reporting the cap is retried, not returned —
    whichever conventional envelope key carries the message, so the generic
    error gate must not let a top-level ``message`` slip through as a completion."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={key: LIMIT})
        return ok_json()

    async with gateway(settings, handler) as (runtime, http, _):
        response = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=chat_body()), timeout=WAIT)
        assert response.status_code == 200, response.text
        assert content_of(response) == "recovered"
        assert LIMIT not in response.text
        assert len(calls) == 2
        assert session_ids(calls) == [SESSION, SESSION]
        assert runtime.chat_concurrency.active_count() == 0


async def test_stream_request_with_json_session_limit_body_is_polled(settings, fast_backoff):
    """A streaming request answered with a JSON cap body is retried, not returned."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"error_msg": LIMIT})
        return ok_sse()

    async with gateway(settings, handler) as (runtime, http, _):
        response = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=chat_body(stream=True)), timeout=WAIT)
        assert response.status_code == 200, response.text
        assert '"content": "recovered"' in response.text
        assert LIMIT not in response.text
        assert len(calls) == 2
        assert session_ids(calls) == [SESSION, SESSION]


@pytest.mark.parametrize(("status", "message"), [
    (400, "bad request"),
    (429, "rate limit exceeded, please slow down"),
], ids=["plain400", "noncap429"])
async def test_plain_400_and_non_cap_429_fail_once_without_polling(settings, fast_backoff, status, message):
    """Only the explicit cap message polls; lookalike statuses fail at once."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error_msg": message})

    async with gateway(settings, handler) as (runtime, http, _):
        response = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=chat_body()), timeout=WAIT)
        assert response.status_code == status, response.text
        assert response.json()["error"]["message"] == message
        await asyncio.sleep(0.1)  # several shrunk backoff rounds would have fired by now
        assert len(calls) == 1, "an ordinary error must never enter the cap polling loop"
        assert runtime.chat_concurrency.active_count() == 0


async def test_session_limit_retry_switches_account_but_keeps_session_id(settings, fast_backoff):
    """An idle alternate account is tried at once; the conversation's upstream
    session id survives the switch (the signing keys prove the accounts differ)."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return limit_json(429)
        return ok_json()

    async with gateway(settings, handler) as (runtime, http, _):
        runtime.pool.add_account(name="second", kind="aksk", ak="other-ak", sk="other-sk")
        response = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=chat_body()), timeout=WAIT)
        assert response.status_code == 200, response.text
        assert content_of(response) == "recovered"
        assert len(calls) == 2
        assert session_ids(calls) == [SESSION, SESSION], "a conversation keeps its upstream session id"
        assert len(set(access_keys(calls))) == 2, "the retry must move to the other eligible account"


async def test_waiting_retry_holds_global_slot_and_newcomer_gets_429(settings, parked_backoff):
    """While the cap persists, the polling request keeps its global slot but
    returns its account slot between polls, so a newcomer at the full gateway
    cap is refused instead of piling up behind it."""
    settings.chat_concurrency_limit = 1
    calls: list[httpx.Request] = []
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if not release.is_set():
            return limit_json(429)
        return ok_json()

    async with gateway(settings, handler) as (runtime, http, _):
        account_id = runtime.pool.accounts[0].id
        first = asyncio.create_task(http.post(CHAT, headers=AUTH, json=chat_body()))
        try:
            await wait_calls(calls, 1, first)
            await wait_account_slot_released(runtime, account_id, first)
            assert runtime.chat_concurrency.active_count() == 1, "a polling request keeps its global slot"

            newcomer = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=chat_body()), timeout=WAIT)
            assert newcomer.status_code == 429, newcomer.text
            assert newcomer.json()["error"]["message"] == "Gateway chat request concurrency limit reached"
            # The parked 0.3s backoff cannot have expired yet, so the free account
            # slot is the steady between-polls state, not a transient between
            # attempts — and the rejected newcomer started nothing upstream.
            assert len(calls) == 1
            assert runtime.chat_concurrency.active_count(account_id) == 0, "polling returns the account slot between attempts"
            assert runtime.chat_concurrency.active_count() == 1

            release.set()
            response = await asyncio.wait_for(first, timeout=WAIT)
            assert response.status_code == 200, response.text
            assert content_of(response) == "recovered"
            assert LIMIT not in response.text
            assert runtime.chat_concurrency.active_count() == 0
        finally:
            await settle(first, release)


async def test_disconnect_during_backoff_stops_polling_and_releases_the_slot(settings, parked_backoff):
    """A client leaving mid-backoff ends the poll: no further upstream calls,
    and both the global and the account slot are returned."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return limit_json(429)

    async with gateway(settings, handler) as (runtime, http, app):
        account_id = runtime.pool.accounts[0].id
        # A controllable native ASGI receive: the client vanishes during a backoff.
        disconnect = asyncio.Event()
        sent: list[dict] = []
        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": json.dumps(chat_body()).encode(), "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        request_task = asyncio.create_task(app({**SCOPE, "state": {}}, receive, send))
        try:
            await wait_calls(calls, 1, request_task)
            await wait_account_slot_released(runtime, account_id, request_task)
            assert runtime.chat_concurrency.active_count() == 1, "the parked poll keeps its global slot"
            assert runtime.chat_concurrency.active_count(account_id) == 0, "the parked poll returned its account slot"

            disconnect.set()
            await asyncio.wait_for(request_task, timeout=WAIT)
            await asyncio.sleep(0.4)  # the abandoned 0.3s backoff would have fired by now
            assert len(calls) == 1, "a disconnected request must stop polling upstream"
            assert runtime.chat_concurrency.active_count() == 0
            assert runtime.chat_concurrency.active_count(account_id) == 0
            assert not any(message["type"] == "http.response.start" for message in sent)
        finally:
            await settle(request_task, disconnect)


async def test_session_limit_after_streamed_content_is_not_replayed(settings, fast_backoff):
    """Once content reached the client the request is never restarted: the cap
    surfaces as one terminal in-band error after the single content frame."""
    calls: list[httpx.Request] = []
    text = sse({"choices": [{"index": 0, "delta": {"content": "partial"}}]}) + sse({"error_msg": LIMIT})

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})

    async with gateway(settings, handler) as (runtime, http, _):
        response = await asyncio.wait_for(http.post(CHAT, headers=AUTH, json=chat_body(stream=True)), timeout=WAIT)
        assert response.status_code == 200, response.text
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        assert frames[0]["choices"][0]["delta"]["content"] == "partial"
        assert response.text.count('"content": "partial"') == 1, "streamed content must never be replayed"
        assert frames[-1]["error"]["code"] == 429
        assert LIMIT in frames[-1]["error"]["message"]
        assert len(calls) == 1, "an error after content was streamed must not restart the request"
        assert runtime.chat_concurrency.active_count() == 0
