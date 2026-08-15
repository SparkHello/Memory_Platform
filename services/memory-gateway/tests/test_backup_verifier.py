from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
VERIFIER = ROOT / "deploy" / "verify_backup.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("deploy_backup_verifier", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_verifier_delegates_to_stack_backup_validator(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_verifier()
    archive = tmp_path / "便携 backup with spaces.zip"
    archive.write_bytes(b"synthetic")
    captured: dict[str, Path] = {}

    def fake_validate(*, archive_path: Path) -> dict[str, object]:
        captured["archive_path"] = archive_path
        return {"ok": True, "restorable": True}

    monkeypatch.setattr(module, "validate_stack_backup", fake_validate)

    assert module.main([str(archive)]) == 0
    assert captured == {"archive_path": archive}
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "restorable": True,
    }


def test_backup_verifier_failure_does_not_reflect_untrusted_path(tmp_path: Path) -> None:
    archive = tmp_path / "do-not-reflect-this-name.zip"
    result = subprocess.run(
        [sys.executable, str(VERIFIER), str(archive)],
        cwd=ROOT / "services" / "memory-gateway",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "backup verification failed: ValueError" in result.stderr
    assert archive.name not in result.stderr
