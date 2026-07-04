from fastapi.testclient import TestClient

import app.main as main
from app.config import get_settings


def test_ui_entrypoints_redirect_or_fall_back_to_index(tmp_path, monkeypatch):
    ui_dist = tmp_path / "dist"
    ui_dist.mkdir()
    (ui_dist / "index.html").write_text("<!doctype html><title>Memory Studio</title>", encoding="utf-8")
    (ui_dist / "assets").mkdir()

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

    get_settings.cache_clear()

