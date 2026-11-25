import pytest

from codearts2api.chat_tool_response import CustomToolStream, restore_completion_tools


def test_restore_completion_custom_input_preserves_json_escaping_and_other_calls():
    completion = {
        "choices": [{"index": 0, "message": {"tool_calls": [
            {"id": "custom-1", "type": "function", "function": {
                "name": "wrapped", "arguments": '{"input":"line 1\\n\\\"quoted\\\""}'
            }},
            {"id": "function-1", "type": "function", "function": {
                "name": "regular", "arguments": "{}"
            }},
        ]}, "finish_reason": "tool_calls"}],
        "usage": {"total_tokens": 4},
    }

    restored = restore_completion_tools(completion, {"wrapped": "lookup"})

    assert restored["choices"][0]["message"]["tool_calls"] == [
        {"id": "custom-1", "type": "custom", "custom": {
            "name": "lookup", "input": 'line 1\n"quoted"'
        }},
        completion["choices"][0]["message"]["tool_calls"][1],
    ]
    assert restored["usage"] == completion["usage"]
    assert completion["choices"][0]["message"]["tool_calls"][0]["type"] == "function"


def test_stream_buffers_fragmented_custom_and_preserves_function_delta():
    stream = CustomToolStream({"wrapped": "lookup"})
    first = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "custom-1", "type": "function", "function": {
            "name": "wrapped", "arguments": '{"in'
        }},
        {"index": 1, "id": "function-1", "type": "function", "function": {
            "name": "regular", "arguments": "{}"
        }},
    ]}, "finish_reason": None}]}
    second = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": 'put":"hello\\nworld"}'}},
    ]}, "finish_reason": None}]}

    assert stream.rewrite(first)["choices"][0]["delta"]["tool_calls"] == [
        first["choices"][0]["delta"]["tool_calls"][1]
    ]
    assert stream.rewrite(second)["choices"][0]["delta"].get("tool_calls") is None
    flushed = stream.flush()
    assert flushed is not None
    assert flushed["choices"] == [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "custom-1", "type": "custom", "custom": {
            "name": "lookup", "input": "hello\nworld"
        }}
    ]}, "finish_reason": None}]
    assert stream.flush() is None


def test_stream_emits_at_finish_and_invalid_wrapper_raises_fixed_error():
    stream = CustomToolStream({"wrapped": "lookup"})
    stream.rewrite({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "custom-1", "function": {
            "name": "wrapped", "arguments": "not-json"
        }}
    ]}, "finish_reason": None}]})
    with pytest.raises(ValueError, match="^Invalid custom tool response$"):
        stream.rewrite({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})


def test_stream_matches_sparse_fragments_by_id_and_flush_omits_usage():
    stream = CustomToolStream({"wrapped": "lookup"})
    first = stream.rewrite({"id": "resp", "object": "chat.completion.chunk", "usage": {"total_tokens": 9},
                            "choices": [{"index": 0, "delta": {"tool_calls": [
                                {"index": 4, "id": "durable", "function": {
                                    "name": "wrapped", "arguments": '{"input":"sparse'
                                }}
                            ]}, "finish_reason": None}]})
    assert "tool_calls" not in first["choices"][0]["delta"]
    stream.rewrite({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"id": "durable", "function": {"arguments": " input\"}"}},
    ]}, "finish_reason": None}]})
    flushed = stream.flush()
    assert flushed is not None
    assert flushed["id"] == "resp"
    assert "usage" not in flushed
    assert flushed["choices"][0]["delta"]["tool_calls"] == [
        {"index": 4, "id": "durable", "type": "custom", "custom": {
            "name": "lookup", "input": "sparse input"
        }}
    ]


def test_missing_custom_id_raises_at_success_but_abort_reason_discards_partial():
    stream = CustomToolStream({"wrapped": "lookup"})
    stream.rewrite({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"name": "wrapped", "arguments": '{"input":"unfinished"}'}},
    ]}, "finish_reason": None}]})
    with pytest.raises(ValueError, match="^Invalid custom tool response$"):
        stream.rewrite({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})

    aborted = CustomToolStream({"wrapped": "lookup"})
    aborted.rewrite({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "custom-1", "function": {"name": "wrapped", "arguments": '{"input":"partial"}'}},
    ]}, "finish_reason": None}]})
    result = aborted.rewrite({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    assert result["choices"][0]["finish_reason"] == "length"
    assert "tool_calls" not in result["choices"][0]["delta"]
    assert aborted.flush() is None
