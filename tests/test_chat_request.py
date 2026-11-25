"""Regressions for OpenCode-to-CodeArts request compatibility."""

from __future__ import annotations

import copy
import json

import pytest

from codearts2api.chat_request import ChatPayloadError, custom_tool_aliases, prepare_chat_payload


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


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash-1017",
        "DeepSeek-V4.1-Flash",
        "codearts/deepseek-v4.1-flash-1017",
    ],
)
def test_verified_deepseek_flash_output_tokens_are_clamped(model: str) -> None:
    for field in ("max_completion_tokens", "max_tokens"):
        result = prepare_chat_payload({"model": model, "messages": [], field: 256000})
        # Only one token field survives the existing preference rules and it
        # must carry the verified upstream limit.
        assert "max_completion_tokens" not in result
        assert result["max_tokens"] == 65536


def test_verified_deepseek_flash_keeps_smaller_absent_and_other_values() -> None:
    for value in (1, 8192, 65535, 65536):
        result = prepare_chat_payload({"model": "deepseek-v4.1-flash", "max_completion_tokens": value})
        assert result["max_tokens"] == value
        assert "max_completion_tokens" not in result

    assert "max_tokens" not in prepare_chat_payload({"model": "deepseek-v4.1-flash-1017", "messages": []})

    # A clamped value keeps the stream options untouched.
    result = prepare_chat_payload({
        "model": "deepseek-v4.1-flash",
        "max_completion_tokens": 256000,
        "stream": True,
        "stream_options": {"include_usage": True},
    })
    assert result["max_tokens"] == 65536
    assert result["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4.1-flash-1024",
        "deepseek-v4.1-flash-x",
        "deepseek-v4.1-flashpro",
        "vendor-deepseek-v4.1-flash",
        "deepseek-v4.1-pro",
        "deepseek-v3.2-volc",
        "M",
    ],
)
def test_other_models_are_not_clamped(model: str) -> None:
    result = prepare_chat_payload({"model": model, "max_completion_tokens": 256000})
    assert result["max_tokens"] == 256000
    assert "max_completion_tokens" not in result


def test_assistant_text_parts_are_flattened_only_when_metadata_free() -> None:
    result = prepare_chat_payload({
        "model": "M",
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "one"}, {"type": "text", "text": " two"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "kept", "cache_control": {"type": "ephemeral"}}]},
        ],
    })
    assert result["messages"][0]["content"] == "one two"
    assert result["messages"][1]["content"] == [{"type": "text", "text": "kept", "cache_control": {"type": "ephemeral"}}]


@pytest.mark.parametrize("model", ["gpt-5", "GPT-5.2", "gpt-6", "gpt-6-turbo"])
def test_gpt_function_tools_disable_reasoning(model: str) -> None:
    result = prepare_chat_payload({
        "model": model,
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "reasoning_effort": "high",
    })
    assert result["reasoning_effort"] == "none"


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-astra-preview", "gpt-6.1-sol", "gpt-6.1-sol-2"])
def test_gpt_reasoning_exceptions_preserve_effort(model: str) -> None:
    result = prepare_chat_payload({
        "model": model,
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "reasoning_effort": "high",
    })
    assert result["reasoning_effort"] == "high"


@pytest.mark.parametrize("model", ["gpt-6-astraX", "gpt-6.1-solX"])
def test_gpt_reasoning_exceptions_require_model_boundary(model: str) -> None:
    result = prepare_chat_payload({
        "model": model,
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "reasoning_effort": "high",
    })
    assert result["reasoning_effort"] == "none"


def test_gpt_reasoning_effort_is_preserved_without_function_tools_or_for_other_families() -> None:
    for payload in (
        {"model": "gpt-5", "reasoning_effort": "high"},
        {"model": "gpt-5", "tools": [{"type": "other"}], "reasoning_effort": "high"},
        {"model": "vendor-gpt-5", "tools": [{"type": "function"}], "reasoning_effort": "high"},
    ):
        assert prepare_chat_payload(payload)["reasoning_effort"] == "high"


def test_custom_tools_convert_with_choice_and_custom_history_without_mutation() -> None:
    payload = {
        "model": "M",
        "tools": [{"type": "custom", "custom": {"name": "shell", "description": "Run command", "format": {"type": "text"}}}],
        "tool_choice": {"type": "custom", "custom": {"name": "shell"}},
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "custom", "custom": {"name": "shell", "input": "打印こんにちは"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "完成"},
        ],
    }
    original = copy.deepcopy(payload)
    aliases = custom_tool_aliases(payload)
    result = prepare_chat_payload(payload, aliases=aliases)
    alias = next(iter(aliases))
    assert result["tools"] == [{
        "type": "function",
        "function": {
            "name": alias,
            "description": "Run command",
            "parameters": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            },
        },
    }]
    assert result["tool_choice"] == {"type": "function", "function": {"name": alias}}
    assert result["messages"][0]["tool_calls"] == [{
        "id": "call-1", "type": "function",
        "function": {"name": alias, "arguments": '{"input":"打印こんにちは"}'},
    }]
    assert result["messages"][1] == payload["messages"][1]
    assert payload == original


def test_custom_aliases_avoid_function_names_and_are_stable() -> None:
    payload = {
        "tools": [
            {"type": "custom", "custom": {"name": "a"}},
            {"type": "custom", "custom": {"name": "b"}},
            {"type": "function", "function": {"name": "custom_" + __import__("hashlib").sha256(b"a").hexdigest()[:12]}},
        ],
    }
    first = custom_tool_aliases(payload)
    assert first == custom_tool_aliases(payload)
    assert set(first.values()) == {"a", "b"}
    assert len(first) == len(set(first))
    assert not set(first).intersection({tool["function"]["name"] for tool in payload["tools"] if tool["type"] == "function"})


def test_custom_type_in_user_data_and_function_schema_is_not_a_custom_tool() -> None:
    payload = {
        "model": "M",
        "messages": [{"role": "user", "content": {"example": {"type": "custom"}}}],
        "tools": [{
            "type": "function",
            "function": {"name": "f", "parameters": {
                "type": "object",
                "properties": {"kind": {"type": "string", "default": {"type": "custom"}}},
            }},
        }],
    }
    result = prepare_chat_payload(payload)
    assert result["messages"] == payload["messages"]
    assert result["tools"] == payload["tools"]


def test_custom_call_requires_text_input() -> None:
    with pytest.raises(ChatPayloadError):
        prepare_chat_payload({"messages": [{"role": "assistant", "tool_calls": [{
            "id": "c", "type": "custom", "custom": {"name": "x", "input": {"unsafe": True}},
        }]}]})


def test_custom_tool_choice_must_reference_a_declared_definition() -> None:
    with pytest.raises(ChatPayloadError, match="Invalid custom tool choice"):
        prepare_chat_payload({
            "tools": [{"type": "custom", "custom": {"name": "defined"}}],
            "tool_choice": {"type": "custom", "custom": {"name": "other"}},
        })


def test_custom_history_call_requires_nonempty_id() -> None:
    with pytest.raises(ChatPayloadError, match="Invalid custom tool call"):
        prepare_chat_payload({"messages": [{"role": "assistant", "tool_calls": [{
            "id": "", "type": "custom", "custom": {"name": "x", "input": "text"},
        }]}]})
