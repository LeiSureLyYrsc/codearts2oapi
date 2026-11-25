"""Normalize OpenAI chat requests for the CodeArts chat endpoint."""

from __future__ import annotations

import copy
import json
import re
from typing import Any


_COMPLETION_TOKEN_MODEL = re.compile(r"^(?:o1|o3|o4|gpt-5|gpt-6)(?:[.-]|$)", re.IGNORECASE)


def _json_string(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _plain_text_parts(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    if not all(
        isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
        and set(part).issubset({"type", "text"})
        for part in value
    ):
        return None
    return "".join(part["text"] for part in value)


def _normalize_message(message: Any, model: str) -> Any:
    if not isinstance(message, dict):
        return message

    result = dict(message)
    role = result.get("role")
    tool_calls = result.get("tool_calls")

    if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
        content = result.get("content")
        # The upstream converter requires text content on tool-call assistant
        # messages; an absent/null value represents an empty text segment.
        if content is None:
            result["content"] = ""

    if role == "tool" and "content" in result and not isinstance(result["content"], str):
        text = _plain_text_parts(result["content"])
        result["content"] = text if text is not None else _json_string(result["content"])

    if isinstance(tool_calls, list):
        normalized_calls = []
        for call in tool_calls:
            if not isinstance(call, dict):
                normalized_calls.append(call)
                continue
            normalized_call = dict(call)
            function = normalized_call.get("function")
            if isinstance(function, dict) and "arguments" in function:
                normalized_function = dict(function)
                arguments = normalized_function["arguments"]
                if not isinstance(arguments, str):
                    normalized_function["arguments"] = _json_string(arguments)
                normalized_call["function"] = normalized_function
            normalized_calls.append(normalized_call)
        result["tool_calls"] = normalized_calls

    if role == "assistant" and model.lower().startswith("deepseek"):
        reasoning_content = result.get("reasoning_content")
        if reasoning_content is None:
            fallback = result.get("reasoning")
            if not isinstance(fallback, str) or not fallback:
                fallback = result.get("reasoning_text")
            result["reasoning_content"] = fallback if isinstance(fallback, str) and fallback else ""

    return result


def prepare_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a safe, upstream-compatible copy of an OpenAI chat request."""
    prepared = copy.deepcopy(payload)
    model = prepared.get("model")
    model_name = model if isinstance(model, str) else ""

    preferred_tokens = "max_completion_tokens" if _COMPLETION_TOKEN_MODEL.search(model_name) else "max_tokens"
    other_tokens = "max_tokens" if preferred_tokens == "max_completion_tokens" else "max_completion_tokens"
    preferred_value = prepared.get(preferred_tokens)
    other_value = prepared.get(other_tokens)
    if preferred_value is None and other_value is not None:
        prepared[preferred_tokens] = other_value
    prepared.pop(other_tokens, None)

    messages = prepared.get("messages")
    if isinstance(messages, list):
        prepared["messages"] = [_normalize_message(message, model_name) for message in messages]

    tools = prepared.get("tools")
    if not tools:
        prepared.pop("tools", None)
        prepared.pop("tool_choice", None)
    elif (
        prepared.get("stream") is True
        and "tool_stream" not in prepared
        and isinstance(tools, list)
        and any(isinstance(tool, dict) and tool.get("type") == "function" for tool in tools)
    ):
        prepared["tool_stream"] = True

    return prepared
