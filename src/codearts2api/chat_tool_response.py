"""Restore custom-tool responses from the upstream function-call wrapper."""

from __future__ import annotations

import copy
import json
from typing import Any


_INVALID = "Invalid custom tool response"
_ABORT_REASONS = {"length", "content_filter"}


def _decode_input(arguments: Any) -> str:
    try:
        wrapper = json.loads(arguments)
    except (TypeError, ValueError) as exc:
        raise ValueError(_INVALID) from exc
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("input"), str):
        raise ValueError(_INVALID)
    return wrapper["input"]


def restore_completion_tools(completion: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    """Convert mapped OpenAI function calls back to native custom tool calls."""
    if not aliases:
        return completion
    result = copy.deepcopy(completion)
    choices = result.get("choices", [])
    if not isinstance(choices, list):
        return result
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        restored = []
        for call in calls:
            if not isinstance(call, dict):
                restored.append(call)
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                restored.append(call)
                continue
            wire_name = function.get("name")
            original_name = aliases.get(wire_name) if isinstance(wire_name, str) else None
            if original_name is None:
                restored.append(call)
                continue
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id or not isinstance(original_name, str) or not original_name:
                raise ValueError(_INVALID)
            restored.append({
                "id": call_id,
                "type": "custom",
                "custom": {"name": original_name, "input": _decode_input(function.get("arguments", ""))},
            })
        message["tool_calls"] = restored
    return result


class CustomToolStream:
    """Buffer mapped wrapper deltas and emit one native custom delta per call."""

    def __init__(self, aliases: dict[str, str]):
        self.aliases = aliases
        self._pending: dict[tuple[int, str], dict[str, Any]] = {}
        self._position: dict[int, int] = {}
        self._last: dict[int, tuple[int, str]] = {}
        self._used_indices: dict[int, set[int]] = {}
        self._envelope: dict[str, Any] = {}

    def _key_for(self, choice_index: int, call: dict[str, Any]) -> tuple[tuple[int, str], int]:
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            for key, item in self._pending.items():
                if key[0] == choice_index and item.get("id") == call_id:
                    return key, item["index"]
        raw_index = call.get("index")
        if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
            index = raw_index
            key = next((key for key, item in self._pending.items()
                        if key[0] == choice_index and item["index"] == index), (choice_index, f"index:{index}"))
            self._used_indices.setdefault(choice_index, set()).add(index)
            self._position[choice_index] = max(self._position.get(choice_index, 0), index + 1)
        else:
            previous = self._last.get(choice_index)
            if call_id is None and previous is not None:
                previous_item = self._pending.get(previous)
                if previous_item is not None and (not previous_item.get("id") or not call_id):
                    return previous, previous_item["index"]
            index = self._position.get(choice_index, 0)
            used = self._used_indices.setdefault(choice_index, set())
            while index in used:
                index += 1
            used.add(index)
            self._position[choice_index] = index + 1
            key = (choice_index, f"position:{index}")
        self._last[choice_index] = key
        return key, index

    def _emit(self, item: dict[str, Any]) -> dict[str, Any]:
        call_id, name = item.get("id"), item.get("name")
        original_name = self.aliases.get(name) if isinstance(name, str) else None
        if not isinstance(call_id, str) or not call_id or not isinstance(original_name, str) or not original_name:
            raise ValueError(_INVALID)
        return {"index": item["index"], "id": call_id, "type": "custom",
                "custom": {"name": original_name, "input": _decode_input(item.get("arguments", ""))}}

    def rewrite(self, chunk: dict[str, Any]) -> dict[str, Any]:
        if not self.aliases:
            return chunk
        result = copy.deepcopy(chunk)
        for field in ("id", "object", "created", "model", "system_fingerprint"):
            if field in result and isinstance(result[field], (str, int, float, type(None))):
                self._envelope[field] = result[field]
        choices = result.get("choices", [])
        if not isinstance(choices, list):
            return result
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            choice_index = choice.get("index", 0)
            if not isinstance(choice_index, int) or isinstance(choice_index, bool):
                choice_index = 0
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = {}
                choice["delta"] = delta
            calls = delta.get("tool_calls")
            if not isinstance(calls, list):
                calls = []
            kept = []
            for call in calls:
                if not isinstance(call, dict):
                    kept.append(call)
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    kept.append(call)
                    continue
                name = function.get("name")
                key_hint = call.get("index")
                is_buffered = isinstance(key_hint, int) and any(
                    key[0] == choice_index and item["index"] == key_hint
                    for key, item in self._pending.items()
                )
                call_id = call.get("id")
                if key_hint is None and not call_id and not name and len(calls) == 1:
                    is_buffered = self._last.get(choice_index) in self._pending
                if isinstance(call_id, str) and call_id:
                    is_buffered = is_buffered or any(
                        key[0] == choice_index and item.get("id") == call_id
                        for key, item in self._pending.items()
                    )
                if not is_buffered and (not isinstance(name, str) or name not in self.aliases):
                    if name or call_id or key_hint is not None:
                        self._last.pop(choice_index, None)
                        self._key_for(choice_index, call)
                        self._last.pop(choice_index, None)
                    kept.append(call)
                    continue
                key, index = self._key_for(choice_index, call)
                item = self._pending.setdefault(key, {"index": index, "arguments": ""})
                if isinstance(call.get("id"), str):
                    item["id"] = call["id"]
                if isinstance(name, str):
                    item["name"] = name
                args = function.get("arguments")
                if isinstance(args, str):
                    item["arguments"] += args
            if len(kept) != len(calls):
                if kept:
                    delta["tool_calls"] = kept
                else:
                    delta.pop("tool_calls", None)
            finish = choice.get("finish_reason")
            if finish is not None:
                pending = [(key, item) for key, item in self._pending.items() if key[0] == choice_index]
                if not isinstance(finish, str):
                    raise ValueError(_INVALID)
                if finish not in _ABORT_REASONS:
                    emitted = [self._emit(item) for _, item in pending]
                    if emitted:
                        delta["tool_calls"] = [*delta.get("tool_calls", []), *emitted]
                for key, _ in pending:
                    del self._pending[key]
                self._last.pop(choice_index, None)
        return result

    def flush(self) -> dict[str, Any] | None:
        if not self._pending:
            return None
        grouped: dict[int, list[dict[str, Any]]] = {}
        for key, item in list(self._pending.items()):
            grouped.setdefault(key[0], []).append(self._emit(item))
            del self._pending[key]
        output = dict(self._envelope)
        output["choices"] = [
            {"index": choice_index, "delta": {"tool_calls": calls}, "finish_reason": None}
            for choice_index, calls in grouped.items()
        ]
        return output
