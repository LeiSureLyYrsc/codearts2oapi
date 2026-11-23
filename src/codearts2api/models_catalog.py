"""Dynamic model list: filter official builtin models and map to OpenAI format."""

from __future__ import annotations

from typing import Any

CODEARTS_PREFIX = "codearts/"


def strip_codearts_prefix(model_id: str) -> str:
    """Return the bare model id. Clients may send a ``codearts/`` prefix."""
    if model_id.startswith(CODEARTS_PREFIX):
        return model_id[len(CODEARTS_PREFIX) :]
    return model_id


def extract_model_records(payload: Any) -> list[dict[str, Any]]:
    """Unwrap common list wrappers without inventing a schema."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (
        "builtinModels",  # actual GET /v1/model/builtin shape: {builtinModels: [...], count: n}
        "models",
        "data",
        "result",
        "list",
        "records",
        "content",
        "builtin",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_model_records(value)
            if nested:
                return nested
    return []


def is_model_exposed(record: dict[str, Any]) -> bool:
    """Expose only models with enable=true.

    When ``display_enabled`` is present it must also be true.
    ``enabled`` is accepted only when ``enable`` is absent (same meaning).
    """
    if "enable" in record:
        if record.get("enable") is not True:
            return False
    elif "enabled" in record:
        if record.get("enabled") is not True:
            return False
    else:
        return False
    if "display_enabled" in record and record.get("display_enabled") is not True:
        return False
    return True


def extract_model_id(record: dict[str, Any]) -> str | None:
    for key in ("id", "model_id", "modelId", "model"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return strip_codearts_prefix(value.strip())
    return None


def to_openai_model(record: dict[str, Any], created: int = 0) -> dict[str, Any] | None:
    model_id = extract_model_id(record)
    if not model_id:
        return None
    owned_by = record.get("owned_by") or record.get("vendor") or record.get("publisher") or "codearts"
    return {
        "id": model_id,
        "object": "model",
        "created": int(record.get("created") or created),
        "owned_by": str(owned_by),
    }


def filter_and_map_models(payload: Any, created: int = 0) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for record in extract_model_records(payload):
        if not is_model_exposed(record):
            continue
        mapped = to_openai_model(record, created=created)
        if mapped is None or mapped["id"] in seen:
            continue
        seen.add(mapped["id"])
        result.append(mapped)
    return result
