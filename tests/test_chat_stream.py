"""Streaming chat transport and terminal-state behavior."""

from __future__ import annotations

import httpx
import json
from fastapi.testclient import TestClient

from codearts2api.app import create_app
from codearts2api.client import SnapAccessClient
from codearts2api.config import Settings


def _client(settings: Settings, upstream: httpx.AsyncClient) -> TestClient:
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    client.app.state.runtime.http = upstream
    client.app.state.runtime.client = SnapAccessClient(settings, upstream, client.app.state.runtime.resolver)
    return client


def _post(client: TestClient):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-local-key"},
        json={"model": "M", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )


def test_http_400_is_json_error_not_sse(settings: Settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = _client(settings, upstream)
    try:
        response = _post(client)
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"]["message"] == "bad request"
    finally:
        client.__exit__(None, None, None)


def test_done_emits_missing_stop_or_tool_finish(settings: Settings) -> None:
    body = (
        'data: {"choices":[{"index":0,"delta":{"content":"hello"}}]}\n\n'
        'data: {"choices":[{"index":1,"delta":{"tool_calls":[{"index":0,"function":{"name":"f"}}]}}]}\n\n'
        "data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = _client(settings, upstream)
    try:
        response = _post(client)
        payloads = [line[6:] for line in response.text.splitlines() if line.startswith("data: {")]
        chunks = [__import__("json").loads(payload) for payload in payloads]
        finishes = {c["choices"][0]["index"]: c["choices"][0]["finish_reason"] for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]}
        assert finishes == {0: "stop", 1: "tool_calls"}
        assert response.text.rstrip().endswith("data: [DONE]")
    finally:
        client.__exit__(None, None, None)


def test_unfinished_eof_emits_error_without_done(settings: Settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = _client(settings, upstream)
    try:
        response = _post(client)
        assert "partial" in response.text
        assert "data: [DONE]" not in response.text
        assert "Upstream stream ended before completion" in response.text
    finally:
        client.__exit__(None, None, None)


def test_clean_eof_after_explicit_finish_is_success(settings: Settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='data: {"id":"chatcmpl-correlated","created":123,"model":"up","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = _client(settings, upstream)
    try:
        response = _post(client)
        chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        assert chunks[-1]["id"] == "chatcmpl-correlated"
        assert chunks[-1]["created"] == 123
        assert chunks[-1]["model"] == "up"
        assert chunks[-1]["choices"][0]["finish_reason"] == "length"
        assert response.text.rstrip().endswith("data: [DONE]")
        assert "Upstream stream ended" not in response.text
    finally:
        client.__exit__(None, None, None)


def test_first_done_terminates_stream_without_repeated_data(settings: Settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=('data: {"id":"id-1","created":7,"model":"M", "choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
                  'data: [DONE]\n\n' 'data: {"choices":[{"index":0,"delta":{"content":"late"}}]}\n\n'),
            headers={"content-type": "text/event-stream"},
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = _client(settings, upstream)
    try:
        response = _post(client)
        assert response.text.count("data: [DONE]") == 1
        assert "late" not in response.text
        chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        inferred = chunks[-1]
        assert inferred["id"] == "id-1"
        assert inferred["created"] == 7
        assert inferred["model"] == "M"
    finally:
        client.__exit__(None, None, None)


def test_multiline_sse_and_mislabeled_ndjson_with_blank_lines(settings: Settings) -> None:
    bodies = [
        'data: {"id":"multi","choices":[\n'
        'data: {"index":0,"delta":{"content":"a"},"finish_reason":"stop"}]}\n\n',
        '{"id":"ndjson","choices":[{"index":0,"delta":{"content":"a"},"finish_reason":"stop"}]}\n\n'
        '{"id":"ndjson-2","choices":[{"index":1,"delta":{"content":"b"},"finish_reason":"stop"}]}\n',
    ]
    for body in bodies:
        async def handler(request: httpx.Request, text: str = body) -> httpx.Response:
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = _client(settings, upstream)
        try:
            response = _post(client)
            assert '"content": "a"' in response.text
            assert "Upstream stream ended" not in response.text
            assert response.text.rstrip().endswith("data: [DONE]")
            if body.startswith("{"):
                assert '"content": "b"' in response.text
        finally:
            client.__exit__(None, None, None)


def test_transport_break_emits_error_without_done(settings: Settings) -> None:
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("connection dropped")

        async def aclose(self) -> None:
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BrokenStream(), headers={"content-type": "text/event-stream"})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = _client(settings, upstream)
    try:
        response = _post(client)
        assert "partial" in response.text
        assert "Upstream stream interrupted" in response.text
        assert "data: [DONE]" not in response.text
    finally:
        client.__exit__(None, None, None)
