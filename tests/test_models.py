"""Model list filtering: enable / display_enabled and prefix stripping."""

from __future__ import annotations

from codearts2api.models_catalog import (
    extract_model_records,
    filter_and_map_models,
    is_model_exposed,
    strip_codearts_prefix,
)


def test_strip_codearts_prefix() -> None:
    assert strip_codearts_prefix("codearts/GLM-5.2") == "GLM-5.2"
    assert strip_codearts_prefix("GLM-5.2") == "GLM-5.2"
    assert strip_codearts_prefix("codearts/") == ""


def test_enable_must_be_true() -> None:
    assert is_model_exposed({"id": "A", "enable": True}) is True
    assert is_model_exposed({"id": "A", "enable": False}) is False
    assert is_model_exposed({"id": "A"}) is False
    assert is_model_exposed({"id": "A", "enabled": True}) is True
    assert is_model_exposed({"id": "A", "enabled": False}) is False


def test_display_enabled_honored_when_present() -> None:
    assert is_model_exposed({"id": "A", "enable": True, "display_enabled": True}) is True
    assert is_model_exposed({"id": "A", "enable": True, "display_enabled": False}) is False
    # Field absent: enable=true is enough.
    assert is_model_exposed({"id": "A", "enable": True}) is True


def test_filter_and_map_unwraps_and_deduplicates() -> None:
    payload = {
        "data": {
            "models": [
                {"id": "codearts/GLM-5.2", "enable": True, "display_enabled": True},
                {"id": "Hidden", "enable": False},
                {"id": "NoDisplay", "enable": True, "display_enabled": False},
                {"model_id": "Visible-2", "enable": True},
                {"id": "GLM-5.2", "enable": True},  # duplicate after prefix strip
            ]
        }
    }
    models = filter_and_map_models(payload, created=1)
    ids = [item["id"] for item in models]
    assert ids == ["GLM-5.2", "Visible-2"]
    assert models[0]["object"] == "model"
    assert models[0]["owned_by"] == "codearts"


# Real GET /v1/model/builtin response (official Windows CLI, provider-models-plugin
# / builtin-model-service): a flat object with a builtinModels array plus a count,
# and records keyed by model_id.
BUILTIN_MODELS_RESPONSE = {
    "builtinModels": [
        {
            "model_id": "GLM-5.2",
            "model_name": "GLM-5.2",
            "vendor": "Zhipu",
            "enable": True,
            "display_enabled": True,
            "supports_images": False,
            "max_input_tokens": 200000,
        },
        {
            "model_id": "Qwen3.6-27B-VL",
            "model_name": "Qwen3.6-27B-VL",
            "vendor": "Qwen",
            "enable": True,
            "display_enabled": True,
            "supports_images": True,
        },
        {
            "model_id": "Retired-Model",
            "model_name": "Retired-Model",
            "vendor": "Qwen",
            "enable": False,
        },
        {
            "model_id": "Internal-Only",
            "model_name": "Internal-Only",
            "vendor": "Zhipu",
            "enable": True,
            "display_enabled": False,
        },
    ],
    "count": 4,
}


def test_extract_model_records_reads_builtin_models_key() -> None:
    records = extract_model_records(BUILTIN_MODELS_RESPONSE)
    assert len(records) == 4
    assert [record["model_id"] for record in records] == [
        "GLM-5.2",
        "Qwen3.6-27B-VL",
        "Retired-Model",
        "Internal-Only",
    ]


def test_filter_and_map_real_builtin_models_response() -> None:
    models = filter_and_map_models(BUILTIN_MODELS_RESPONSE, created=42)
    assert [item["id"] for item in models] == ["GLM-5.2", "Qwen3.6-27B-VL"]
    for item in models:
        assert item["object"] == "model"
        assert item["created"] == 42
    assert models[0]["owned_by"] == "Zhipu"
    assert models[1]["owned_by"] == "Qwen"
