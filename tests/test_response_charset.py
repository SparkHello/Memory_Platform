from fastapi.testclient import TestClient

from app.main import create_app


def test_json_responses_declare_utf8_charset() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
