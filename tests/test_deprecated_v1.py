from fastapi.testclient import TestClient


def test_v1_models_returns_gone(
    client: TestClient,
) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "v1_gateway_deprecated"


def test_v1_chat_completions_returns_gone(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "legacy-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "v1_gateway_deprecated"
