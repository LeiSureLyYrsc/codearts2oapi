"""Normalize OpenAI chat requests for the CodeArts chat endpoint."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from codearts2api.models_catalog import strip_codearts_prefix


_COMPLETION_TOKEN_MODEL = re.compile(r"^(?:o1|o3|o4|gpt-5|gpt-6)(?:[.-]|$)", re.IGNORECASE)
_GPT_MODEL = re.compile(r"^gpt-(?:5|6)(?:[.-]|$)", re.IGNORECASE)

# Exact builtin CodeArts model ids that declare ``max_tokens: 65536`` in the
# live catalog; larger ``max_tokens`` requests fail with InferHub.001001005.400.
# Matched case-insensitively against the bare, prefix-stripped model id.
_OUTPUT_TOKEN_LIMITS = {
    "deepseek-v4.1-flash": 65536,
    "deepseek-v4.1-flash-1017": 65536,
}


class ChatPayloadError(ValueError):
    """A request contains a feature the upstream chat API cannot represent."""


def _json_string(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _custom_tool_name(custom: Any) -> str:
    if not isinstance(custom, dict) or not isinstance(custom.get("name"), str) or not custom["name"]:
        raise ChatPayloadError("Invalid custom tool definition.")
    return custom["name"]


def _custom_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "custom":
                names.add(_custom_tool_name(tool.get("custom")))
    choice = payload.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "custom":
        names.add(_custom_tool_name(choice.get("custom")))
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
                continue
            for call in message["tool_calls"]:
                if isinstance(call, dict) and call.get("type") == "custom":
                    names.add(_custom_tool_name(call.get("custom")))
    return names


def custom_tool_aliases(payload: dict[str, Any]) -> dict[str, str]:
    """Map generated function names back to source custom tool names."""
    names = _custom_names(payload)
    if not names:
        return {}

    occupied: set[str] = set()
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "function":
                function = tool.get("function")
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    occupied.add(function["name"])
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
                continue
            for call in message["tool_calls"]:
                if isinstance(call, dict) and call.get("type") == "function":
                    function = call.get("function")
                    if isinstance(function, dict) and isinstance(function.get("name"), str):
                        occupied.add(function["name"])

    aliases: dict[str, str] = {}
    for original in sorted(names):
        stem = f"custom_{hashlib.sha256(original.encode('utf-8')).hexdigest()[:12]}"
        alias = stem
        suffix = 2
        while alias in occupied:
            alias = f"{stem}_{suffix}"
            suffix += 1
        occupied.add(alias)
        aliases[alias] = original
    return aliases


def _custom_description(custom: dict[str, Any]) -> str | None:
    description = custom.get("description")
    if description is not None and not isinstance(description, str):
        raise ChatPayloadError("Invalid custom tool definition.")
    tool_format = custom.get("format")
    if tool_format is None or tool_format == {"type": "text"}:
        return description
    if (
        not isinstance(tool_format, dict)
        or not isinstance(tool_format.get("type"), str)
        or tool_format.get("type") not in {"text", "grammar"}
    ):
        raise ChatPayloadError("Invalid custom tool format.")
    # Keep format constraints descriptive; never compile or execute supplied grammars.
    format_note = (
        "Follow this output grammar: " + _json_string(tool_format)
        if tool_format.get("type") == "grammar"
        else "Output format: " + _json_string(tool_format)
    )
    return f"{description}\n{format_note}" if description else format_note


def _custom_function_tool(tool: dict[str, Any], aliases_by_original: dict[str, str]) -> dict[str, Any]:
    custom = tool.get("custom")
    name = _custom_tool_name(custom)
    assert isinstance(custom, dict)
    alias = aliases_by_original.get(name)
    if alias is None:
        raise ChatPayloadError("Invalid custom tool definition.")
    description = _custom_description(custom)
    function: dict[str, Any] = {
        "name": alias,
        "parameters": {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
            "additionalProperties": False,
        },
    }
    if description is not None:
        function["description"] = description
    return {**{key: value for key, value in tool.items() if key != "custom"}, "type": "function", "function": function}


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


def _normalize_message(message: Any, model: str, aliases_by_original: dict[str, str]) -> Any:
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

    if role == "assistant" and "content" in result:
        text = _plain_text_parts(result["content"])
        if text is not None:
            result["content"] = text

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
            if call.get("type") == "custom":
                custom = call.get("custom")
                name = _custom_tool_name(custom)
                assert isinstance(custom, dict)
                alias = aliases_by_original.get(name)
                if (
                    alias is None
                    or not isinstance(call.get("id"), str)
                    or not call["id"]
                    or not isinstance(custom.get("input"), str)
                ):
                    raise ChatPayloadError("Invalid custom tool call.")
                normalized_call["type"] = "function"
                normalized_call.pop("custom", None)
                normalized_call["function"] = {
                    "name": alias,
                    "arguments": _json_string({"input": custom["input"]}),
                }
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


def prepare_chat_payload(
    payload: dict[str, Any], *, aliases: dict[str, str] | None = None
) -> dict[str, Any]:
    """Return a safe, upstream-compatible copy of a chat-completions request."""
    prepared = copy.deepcopy(payload)
    aliases = custom_tool_aliases(payload) if aliases is None else aliases
    aliases_by_original = {original: wire for wire, original in aliases.items()}
    model = prepared.get("model")
    model_name = model if isinstance(model, str) else ""

    messages = prepared.get("messages")
    tools = prepared.get("tools")
    custom_definitions: set[str] = set()
    function_tools = isinstance(tools, list) and any(
        isinstance(tool, dict) and tool.get("type") == "function" for tool in tools
    )
    if isinstance(tools, list):
        custom_definitions = {
            _custom_tool_name(tool.get("custom"))
            for tool in tools
            if isinstance(tool, dict) and tool.get("type") == "custom"
        }
        prepared["tools"] = [
            _custom_function_tool(tool, aliases_by_original) if isinstance(tool, dict) and tool.get("type") == "custom" else tool
            for tool in tools
        ]
        tools = prepared["tools"]
        function_tools = any(isinstance(tool, dict) and tool.get("type") == "function" for tool in tools)
    choice = prepared.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "custom":
        custom = choice.get("custom")
        name = _custom_tool_name(custom)
        alias = aliases_by_original.get(name)
        if alias is None or name not in custom_definitions:
            raise ChatPayloadError("Invalid custom tool choice.")
        prepared["tool_choice"] = {"type": "function", "function": {"name": alias}}

    preferred_tokens = "max_completion_tokens" if _COMPLETION_TOKEN_MODEL.search(model_name) else "max_tokens"
    other_tokens = "max_tokens" if preferred_tokens == "max_completion_tokens" else "max_completion_tokens"
    preferred_value = prepared.get(preferred_tokens)
    other_value = prepared.get(other_tokens)
    if preferred_value is None and other_value is not None:
        prepared[preferred_tokens] = other_value
    prepared.pop(other_tokens, None)
    limit = _OUTPUT_TOKEN_LIMITS.get(strip_codearts_prefix(model_name).strip().lower())
    if limit is not None:
        selected = prepared.get(preferred_tokens)
        if isinstance(selected, int) and not isinstance(selected, bool) and selected > limit:
            prepared[preferred_tokens] = limit

    if isinstance(messages, list):
        prepared["messages"] = [
            _normalize_message(message, model_name, aliases_by_original) for message in messages
        ]

    if not tools:
        prepared.pop("tools", None)
        prepared.pop("tool_choice", None)
    elif (
        prepared.get("stream") is True
        and "tool_stream" not in prepared
        and function_tools
    ):
        prepared["tool_stream"] = True

    model_lower = model_name.lower()
    gpt_reasoning_exception = re.match(r"^gpt-6-astra(?:[.-]|$)", model_lower) or re.match(
        r"^gpt-6\.1-sol(?:[.-]|$)", model_lower
    )
    if function_tools and _GPT_MODEL.match(model_name) and not gpt_reasoning_exception:
        prepared["reasoning_effort"] = "none"

    return prepared
