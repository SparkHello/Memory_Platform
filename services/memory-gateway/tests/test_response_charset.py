from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def test_json_responses_declare_utf8_charset(tmp_path, monkeypatch) -> None:
    # 必须隔离到临时库：create_app 的 lifespan 会对默认 data/*.db 执行 init_db。
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("KNOWLEDGE_DATABASE_PATH", str(tmp_path / "knowledge.db"))
    monkeypatch.setenv("GATEWAY_API_KEY", "test-key")
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    get_settings.cache_clear()
