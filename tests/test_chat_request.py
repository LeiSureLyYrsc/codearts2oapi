"""Regressions for OpenCode-to-CodeArts request compatibility."""

from __future__ import annotations

import copy
import json

from codearts2api.chat_request import prepare_chat_payload


def test_opencode_default_and_tool_followup_payload() -> None:
    payload = {
        "model": "deepseek-v3",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Find the weather"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": {"city": "Shanghai"}}},
                    {"id": "call_2", "type": "function", "function": {"name": "clock", "arguments": "{\"tz\":\"Asia/Shanghai\"}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": {"temp": 22}},
            {"role": "tool", "tool_call_id": "call_2", "content": "12:00"},
        ],
        "max_completion_tokens": 4096,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tools": [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}],
        "tool_choice": "auto",
    }
    original = copy.deepcopy(payload)

    result = prepare_chat_payload(payload)

    assert result["max_tokens"] == 4096
    assert "max_completion_tokens" not in result
    assert result["messages"][0]["content"] == payload["messages"][0]["content"]
    assistant = result["messages"][1]
    assert assistant["content"] == ""
    assert assistant["reasoning_content"] == ""
    assert [call["id"] for call in assistant["tool_calls"]] == ["call_1", "call_2"]
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"city": "Shanghai"}
    assert assistant["tool_calls"][1]["function"]["arguments"] == '{"tz":"Asia/Shanghai"}'
    assert [message["tool_call_id"] for message in result["messages"][2:]] == ["call_1", "call_2"]
    assert result["messages"][2]["content"] == '{"temp":22}'
    assert result["messages"][3]["content"] == "12:00"
    assert result["tool_stream"] is True
    assert result["stream_options"] == payload["stream_options"]
    assert payload == original


def test_model_token_field_boundary_and_preference() -> None:
    assert prepare_chat_payload({"model": "vendor-gpt-5", "max_tokens": 100})["max_tokens"] == 100
    result = prepare_chat_payload({"model": "gpt-5.2", "max_tokens": 100, "max_completion_tokens": 200})
    assert result["max_completion_tokens"] == 200
    assert "max_tokens" not in result
    result = prepare_chat_payload({"model": "custom-gpt-6", "max_tokens": 100})
    assert result["max_tokens"] == 100
    result = prepare_chat_payload({"model": "gpt-4.1", "max_tokens": 100, "max_completion_tokens": 200})
    assert result["max_tokens"] == 100
    assert "max_completion_tokens" not in result


def test_valid_tool_argument_string_and_existing_reasoning_are_preserved() -> None:
    payload = {
        "model": "deepseek-r1",
        "messages": [{
            "role": "assistant",
            "reasoning_content": "thinking",
            "tool_calls": [{"id": "c", "function": {"arguments": "{\"x\":1}"}}],
        }],
        "stream": True,
        "tool_stream": False,
        "tools": [{"type": "function", "function": {"name": "f"}}],
    }
    result = prepare_chat_payload(payload)
    assert result["messages"][0]["reasoning_content"] == "thinking"
    assert result["messages"][0]["tool_calls"][0]["function"]["arguments"] == '{"x":1}'
    assert result["tool_stream"] is False


def test_tool_text_parts_and_reasoning_fallback_are_normalized() -> None:
    result = prepare_chat_payload({
        "model": "deepseek-v3",
        "messages": [
            {"role": "tool", "tool_call_id": "c", "content": [{"type": "text", "text": "first"}, {"type": "text", "text": " second"}]},
            {"role": "tool", "tool_call_id": "d", "content": [{"type": "text", "text": "kept", "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "reasoning": "use this", "content": "answer"},
            {"role": "assistant", "reasoning_content": "already present", "reasoning": "ignore this"},
        ],
    })
    assert result["messages"][0]["content"] == "first second"
    assert json.loads(result["messages"][1]["content"]) == [{"type": "text", "text": "kept", "cache_control": {"type": "ephemeral"}}]
    assert result["messages"][2]["reasoning_content"] == "use this"
    assert result["messages"][3]["reasoning_content"] == "already present"


def test_deepseek_reasoning_prefix_is_case_insensitive() -> None:
    result = prepare_chat_payload({
        "model": "DeepSeek-V4-Flash",
        "messages": [{"role": "assistant", "content": "answer", "reasoning": "thinking"}],
    })
    assert result["messages"][0]["reasoning_content"] == "thinking"


def test_invalid_tool_argument_string_is_preserved_verbatim() -> None:
    value = "{partial"
    result = prepare_chat_payload({
        "model": "M",
        "messages": [{"role": "assistant", "tool_calls": [{"id": "c", "function": {"arguments": value}}]}],
    })
    assert result["messages"][0]["tool_calls"][0]["function"]["arguments"] == value


def test_empty_tools_are_removed_without_changing_other_selection_fields() -> None:
    result = prepare_chat_payload({"model": "M", "tools": [], "tool_choice": "auto", "reasoning_effort": "high"})
    assert "tools" not in result
    assert "tool_choice" not in result
    assert result["reasoning_effort"] == "high"
