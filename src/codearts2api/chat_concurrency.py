"""Process-local concurrency tracking, bounded FIFO queueing, and operator cancellation."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from enum import Enum
from typing import Any

import anyio
from starlette._utils import get_route_path

# Queue policy. A waiting request holds no global or account slot, so these
# bounds only cap memory and how long one HTTP request may stay parked.
# Tests may monkeypatch either name, e.g.
# ``monkeypatch.setattr("codearts2api.chat_concurrency.QUEUE_WAIT_TIMEOUT_SECONDS", 0.05)``.
QUEUE_MAX_WAITERS = 128
QUEUE_WAIT_TIMEOUT_SECONDS = 60.0


class Capacity(str, Enum):
    """What a capacity probe found.

    ``reserve()`` both decides and takes: return ``READY`` only after
    ``ticket.try_reserve`` succeeded, so ``READY`` already owns a global and an
    account slot. ``BUSY`` must leave the registry untouched.
    """

    READY = "ready"              # both a global and an account slot were taken
    BUSY = "busy"                # candidates exist, but no slot is free right now
    UNAVAILABLE = "unavailable"  # no usable candidate account at all


class QueueResult(str, Enum):
    """Why a queued request stopped waiting."""

    ACQUIRED = "acquired"
    QUEUE_FULL = "queue_full"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    DISCONNECTED = "disconnected"
    NO_CAPACITY = "no_capacity"


class ChatRequestTicket:
    """A single chat request's global and (at most one) account slot."""

    def __init__(self, registry: "ChatConcurrency") -> None:
        self._registry = registry
        self._cancel_scope: anyio.CancelScope | None = None
        self._admitted = False
        self._account_id: str | None = None
        self._released = False
        # Set by a receive watcher; response messages are dropped afterwards
        # because no client is left to read them.
        self.peer_disconnected = False

    @property
    def account_id(self) -> str | None:
        """The account slot this request currently holds, if any."""
        return self._account_id

    @property
    def released(self) -> bool:
        return self._released

    def admit(self, global_limit: int) -> bool:
        """Atomically claim a global request slot; zero means unlimited."""
        if self._released:
            return False
        if self._admitted:
            return True
        if global_limit > 0 and self._registry.active_count() >= global_limit:
            return False
        self._admitted = True
        self._registry._global_count += 1
        return True

    def bind(self, account_id: str, account_limit: int) -> bool:
        """Claim this request's account slot before an upstream attempt."""
        if self._released or not self._admitted:
            return False
        if self._account_id == account_id:
            return True
        if self._account_id is not None:
            return False
        count = self._registry._account_counts[account_id]
        if account_limit > 0 and count >= account_limit:
            return False
        self._account_id = account_id
        self._registry._account_counts[account_id] += 1
        return True

    def unbind(self) -> None:
        """Release the current account slot while retaining the global slot."""
        account_id, self._account_id = self._account_id, None
        if account_id is not None:
            self._registry._drop_account(account_id)

    def try_reserve(
        self,
        global_limit: int,
        candidates: Callable[[], Iterable[tuple[str, int]]],
    ) -> bool:
        """Take a global slot plus the first idle account slot in one atomic step.

        Either both slots are held afterwards or nothing changed, so a queued
        request never consumes an active slot. ``candidates`` runs only after
        the global slot is held and only when no account slot is held yet; it
        must be synchronous so nothing interleaves between check and take.
        """
        if self._released:
            return False
        if self._account_id is not None:
            return True
        if not self.admit(global_limit):
            return False
        for account_id, account_limit in candidates():
            if self.bind(account_id, account_limit):
                return True
        self.release_admission()
        return False

    def release_admission(self) -> None:
        """Give the global slot back when no account slot is held."""
        if self._account_id is not None or not self._admitted:
            return
        self._admitted = False
        self._registry._drop_global()

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        self.unbind()
        if self._admitted:
            self._admitted = False
            self._registry._drop_global()


class _QueueWaiter:
    """One request parked until a global plus account slot is available.

    The event is replaced after every wake-up because :meth:`ChatConcurrency.notify`
    only ever sets it; recreating it keeps each wake-up one-shot.
    """

    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = anyio.Event()


class ChatConcurrency:
    """In-memory registry. Calls are synchronous and atomic on the ASGI loop."""

    def __init__(self) -> None:
        self._global_count = 0
        self._account_counts: defaultdict[str, int] = defaultdict(int)
        self._tickets: set[ChatRequestTicket] = set()
        self._waiters: deque[_QueueWaiter] = deque()

    def active_count(self, account_id: str | None = None) -> int:
        return self._account_counts.get(account_id, 0) if account_id is not None else self._global_count

    def waiting_count(self) -> int:
        """Queued requests; they hold no global or account slot."""
        return len(self._waiters)

    def notify(self) -> None:
        """Let the queue head re-check after capacity or limits change.

        Only the head is woken because it is the single request allowed to take
        a slot; waking the rest would just add a herd. Spurious wakes are
        harmless: the head re-tests the predicate before taking anything.
        """
        if self._waiters:
            self._waiters[0].event.set()

    def begin_wait(
        self,
        ticket: ChatRequestTicket,
        reserve: Callable[[], Capacity],
        *,
        max_waiters: int | None = None,
    ) -> QueueResult | _QueueWaiter:
        """Decide synchronously: admit now, refuse, or take a queue position.

        Joining the tail before any await is what makes the queue fair: no
        newcomer can slip in between "this request must wait" and its position.
        """
        if ticket.released:
            return QueueResult.CANCELLED
        if not self._waiters:
            state = reserve()
            if state is Capacity.READY:
                return QueueResult.ACQUIRED
            if state is Capacity.UNAVAILABLE:
                return QueueResult.NO_CAPACITY
        limit = QUEUE_MAX_WAITERS if max_waiters is None else max_waiters
        if len(self._waiters) >= limit:
            return QueueResult.QUEUE_FULL
        waiter = _QueueWaiter()
        self._waiters.append(waiter)
        return waiter

    async def run_waiter(
        self,
        ticket: ChatRequestTicket,
        waiter: _QueueWaiter,
        reserve: Callable[[], Capacity],
        *,
        timeout: float | None = None,
    ) -> QueueResult:
        """Wait for the queue position to reach the head and for capacity to exist.

        Only the head calls ``reserve()``, so an equal claim is never overtaken.
        A deadline that has already passed ends the wait instead of starting an
        upstream attempt after it expired.
        """
        wait_seconds = QUEUE_WAIT_TIMEOUT_SECONDS if timeout is None else timeout
        deadline = anyio.current_time() + wait_seconds
        try:
            with anyio.move_on_after(wait_seconds):
                while True:
                    if ticket.released:
                        return QueueResult.CANCELLED
                    if self._waiters[0] is waiter:
                        if anyio.current_time() >= deadline:
                            return QueueResult.TIMED_OUT
                        state = reserve()
                        if state is Capacity.READY:
                            return QueueResult.ACQUIRED
                        if state is Capacity.UNAVAILABLE:
                            return QueueResult.NO_CAPACITY
                    waiter.event = anyio.Event()
                    await waiter.event.wait()
        finally:
            self.abandon_wait(waiter)
        return QueueResult.TIMED_OUT

    def abandon_wait(self, waiter: _QueueWaiter) -> None:
        """Drop a queue position; idempotent, and wakes the next request."""
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        self.notify()

    def cancel_account(self, account_id: str) -> int:
        """Cancel active requests currently bound to account; repeated calls are safe.

        Queued requests hold no account slot yet, so they are not selected: the
        operator cancel targets work that is already on the wire.
        """
        selected = [ticket for ticket in self._tickets
                    if ticket._account_id == account_id
                    and ticket._cancel_scope is not None
                    and not ticket._cancel_scope.cancel_called]
        for ticket in selected:
            if ticket._cancel_scope is not None:
                ticket._cancel_scope.cancel()
        return len(selected)

    def _drop_global(self) -> None:
        self._global_count -= 1
        self.notify()

    def _drop_account(self, account_id: str) -> None:
        count = self._account_counts[account_id] - 1
        if count > 0:
            self._account_counts[account_id] = count
        else:
            self._account_counts.pop(account_id, None)
        self.notify()


async def wait_for_capacity_or_disconnect(
    ticket: ChatRequestTicket,
    reserve: Callable[[], Capacity],
    receive: Callable[[], Awaitable[dict[str, Any]]],
    *,
    timeout: float | None = None,
    max_waiters: int | None = None,
) -> QueueResult:
    """Admit now, else queue for capacity while watching for the client leaving.

    The immediate path never starts a task, so an unconstrained gateway keeps its
    previous single-coroutine behaviour. While parked, the request body is
    already parsed, so ``receive`` only waits for the next client message. The
    watcher starts first and the caller waits on the queue itself, so a
    disconnect cancels the wait before any upstream attempt starts; the watcher
    stops on exit, leaving ``receive`` to the response code afterwards.
    """
    registry = ticket._registry
    claim = registry.begin_wait(ticket, reserve, max_waiters=max_waiters)
    if isinstance(claim, QueueResult):
        return claim
    disconnected = False
    result = QueueResult.TIMED_OUT
    try:
        async with anyio.create_task_group() as group:
            async def watch() -> None:
                nonlocal disconnected
                while True:
                    message = await receive()
                    if message.get("type") == "http.disconnect":
                        disconnected = True
                        ticket.peer_disconnected = True
                        group.cancel_scope.cancel()
                        return

            group.start_soon(watch)
            try:
                result = await registry.run_waiter(ticket, claim, reserve, timeout=timeout)
            finally:
                # Stop the watcher on every exit path (acquired, timed out,
                # cancelled). Leaving it parked in receive() would make the
                # task group wait for it forever and hang the response.
                group.cancel_scope.cancel()
    finally:
        # Idempotent: covers both a normal return and a wait that never started
        # because the watcher cancelled the group first.
        registry.abandon_wait(claim)
    if disconnected:
        # A reservation can land in the same round as the disconnect; a request
        # nobody is waiting for must not keep the slot it just took.
        ticket.unbind()
        ticket.release_admission()
        return QueueResult.DISCONNECTED
    return result


class ChatConcurrencyMiddleware:
    """Attach a ticket to POST /v1/chat/completions and contain operator cancel."""

    def __init__(self, app: Any, registry: ChatConcurrency) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or get_route_path(scope) != "/v1/chat/completions"):
            await self.app(scope, receive, send)
            return

        ticket = ChatRequestTicket(self.registry)
        self.registry._tickets.add(ticket)
        ticket._cancel_scope = anyio.CancelScope()
        started = False
        completed = False
        content_type = ""
        disconnected = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal started, completed, content_type
            if disconnected or ticket.peer_disconnected:
                # The client is gone; a queued request that never started must
                # not emit response.start, and nobody reads the rest.
                return
            if message["type"] == "http.response.start":
                await send(message)
                started = True
                content_type = next((value.decode("latin-1").lower() for key, value in message.get("headers", [])
                                     if key.lower() == b"content-type"), "")
                return
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        async def tracked_receive() -> dict[str, Any]:
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        try:
            state = scope.setdefault("state", {})
            state["chat_request"] = ticket
            with ticket._cancel_scope:
                await self.app(scope, tracked_receive, tracked_send)
            if ticket._cancel_scope.cancel_called and not disconnected and not ticket.peer_disconnected and not completed:
                if not started:
                    payload = {"error": {"message": "Request cancelled by operator", "type": "request_cancelled", "code": 409}}
                    body = json.dumps(payload, separators=(",", ":")).encode()
                    await send({"type": "http.response.start", "status": 409,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body, "more_body": False})
                elif "text/event-stream" in content_type:
                    payload = {"error": {"message": "Request cancelled by operator",
                                          "type": "request_cancelled", "code": 409}}
                    body = ("data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()
                    await send({"type": "http.response.body", "body": body, "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            ticket._release()
            self.registry._tickets.discard(ticket)
