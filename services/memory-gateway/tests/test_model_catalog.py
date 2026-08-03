from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from app.config import Settings
from app.model_catalog import (
    BUILTIN_CATALOG_PATH,
    BUILTIN_ROUTES_PATH,
    CatalogError,
    load_model_catalog,
    providers_for_operation,
    providers_for_route,
)


def test_embedding_provider_requires_embedding_kind(tmp_path) -> None:
    catalog_path = tmp_path / "models.json"
    catalog_path.write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "id": "embedding/not-a-chat-model",
                        "provider": "embedding",
                        "model": "not-a-chat-model",
                        "kind": "chat",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="embedding provider"):
        load_model_catalog(catalog_path)


def test_external_feature_routes_select_exact_model_variants(tmp_path) -> None:
    catalog_path = tmp_path / "models.json"
    routes_path = tmp_path / "routes.json"
    shutil.copyfile(BUILTIN_CATALOG_PATH, catalog_path)
    routes = json.loads(BUILTIN_ROUTES_PATH.read_text(encoding="utf-8"))
    routes["routes"]["memory.review"] = [
        "kimi/kimi-k2.7-code-highspeed",
        "deepseek/deepseek-v4-flash",
    ]
    routes_path.write_text(json.dumps(routes), encoding="utf-8")
    settings = Settings(
        _env_file=None,
        MODEL_CATALOG_PATH=str(catalog_path),
        MODEL_ROUTES_PATH=str(routes_path),
        LLM_KIMI_API_KEY="kimi-key",
        LLM_DEEPSEEK_API_KEY="deepseek-key",
    )

    providers = providers_for_operation(settings, "memory-review-editor")

    assert [provider.model for provider in providers] == [
        "kimi-k2.7-code-highspeed",
        "deepseek-v4-flash",
    ]


def test_chat_route_skips_models_without_provider_keys(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        MODEL_CATALOG_PATH=str(BUILTIN_CATALOG_PATH),
        MODEL_ROUTES_PATH=str(BUILTIN_ROUTES_PATH),
        LLM_MIMO_API_KEY="",
        LLM_KIMI_API_KEY="kimi-key",
        LLM_DEEPSEEK_API_KEY="",
        UPSTREAM_API_KEY="",
    )

    providers = providers_for_route(settings, "chat")

    assert [provider.code for provider in providers] == ["K"]
    assert [provider.model for provider in providers] == ["kimi-k2.7-code"]
