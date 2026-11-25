"""Process-local concurrency tracking and operator cancellation for chat requests."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import anyio
from starlette._utils import get_route_path


class ChatRequestTicket:
    """A single chat request's global and (at most one) account slot."""

    def __init__(self, registry: "ChatConcurrency") -> None:
        self._registry = registry
        self._cancel_scope: anyio.CancelScope | None = None
        self._admitted = False
        self._account_id: str | None = None
        self._released = False

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

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        self.unbind()
        if self._admitted:
            self._admitted = False
            self._registry._global_count -= 1


class ChatConcurrency:
    """In-memory registry. Calls are synchronous and atomic on the ASGI loop."""

    def __init__(self) -> None:
        self._global_count = 0
        self._account_counts: defaultdict[str, int] = defaultdict(int)
        self._tickets: set[ChatRequestTicket] = set()

    def active_count(self, account_id: str | None = None) -> int:
        return self._account_counts.get(account_id, 0) if account_id is not None else self._global_count

    def cancel_account(self, account_id: str) -> int:
        """Cancel active requests currently bound to account; repeated calls are safe."""
        selected = [ticket for ticket in self._tickets
                    if ticket._account_id == account_id
                    and ticket._cancel_scope is not None
                    and not ticket._cancel_scope.cancel_called]
        for ticket in selected:
            if ticket._cancel_scope is not None:
                ticket._cancel_scope.cancel()
        return len(selected)

    def _drop_account(self, account_id: str) -> None:
        count = self._account_counts[account_id] - 1
        if count > 0:
            self._account_counts[account_id] = count
        else:
            self._account_counts.pop(account_id, None)


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
            if ticket._cancel_scope.cancel_called and not disconnected and not completed:
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
