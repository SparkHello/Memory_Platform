from pathlib import Path

from model_gateway.config_store import gateway_paths


def test_gateway_secrets_can_live_on_a_separate_volume(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "model-data"
    secret_path = tmp_path / "model-secrets" / "secrets.env"
    monkeypatch.setenv("MODEL_GATEWAY_SECRETS_PATH", str(secret_path))

    paths = gateway_paths(home)

    assert paths.home == home
    assert paths.config == home / "config.json"
    assert paths.usage_db == home / "usage.db"
    assert paths.secrets == secret_path

