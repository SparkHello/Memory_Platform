from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import get_settings


def test_ui_entrypoints_redirect_or_fall_back_to_index(tmp_path, monkeypatch):
    ui_dist = tmp_path / "dist"
    ui_dist.mkdir()
    (ui_dist / "index.html").write_text("<!doctype html><title>Memory Studio</title>", encoding="utf-8")
    assets = ui_dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('safe asset')", encoding="utf-8")
    (ui_dist / "README.md").write_text("must not be served", encoding="utf-8")
    (ui_dist / ".env").write_text("SECRET=must-not-leak", encoding="utf-8")
    (ui_dist / "LICENSE").write_text("must not be served", encoding="utf-8")
    data_dir = ui_dist / "data"
    data_dir.mkdir()
    (data_dir / "memory.db").write_bytes(b"must not be served")

    monkeypatch.setattr(main, "UI_DIST_DIR", ui_dist)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("GATEWAY_API_KEY", "test-gateway-key")
    get_settings.cache_clear()

    with TestClient(main.create_app()) as client:
        for path in ["/", "/dashboard", "/studio", "/memory-studio", "/记忆工作室", "/ui"]:
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["location"] == "/ui/"

        for path in ["/ui/", "/ui/dashboard", "/ui/记忆工作室"]:
            response = client.get(path)
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]

        response = client.get("/ui/assets/missing.js")
        assert response.status_code == 404

        response = client.get("/ui/assets/app.js")
        assert response.status_code == 200
        assert "safe asset" in response.text

        for path in ["/ui/README.md", "/ui/.env", "/ui/data/memory.db"]:
            response = client.get(path)
            assert response.status_code == 404

        response = client.get("/ui/LICENSE")
        assert response.status_code == 200
        assert "must not be served" not in response.text
        assert "Memory Studio" in response.text

    get_settings.cache_clear()


def test_configured_ui_dist_dir_must_be_a_vite_build(tmp_path):
    invalid = tmp_path / "not-a-build"
    invalid.mkdir()
    (invalid / "index.html").write_text("<!doctype html>", encoding="utf-8")

    with pytest.raises(RuntimeError, match="UI_DIST_DIR"):
        main._resolve_ui_dist_dir(SimpleNamespace(ui_dist_dir=str(invalid)))

    assets = invalid / "assets"
    assets.mkdir()
    assert main._resolve_ui_dist_dir(
        SimpleNamespace(ui_dist_dir=str(invalid))
    ) == invalid.resolve()
