"""Offline restore helper for a stopped split-container stack."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from app.cli_config import CliPaths
from app.stack_backup import restore_stack_backup


MEMORY_UID = 10001
MODEL_UID = 10002
MEMORY_DATA = Path("/data")
MEMORY_SETTINGS = Path("/secrets/settings.env")
MODEL_DATA = Path("/model-data")
ARCHIVE = Path(os.getenv("RESTORE_ARCHIVE", "/data/restore.zip"))


def main() -> int:
    os.umask(0o077)
    paths = CliPaths(
        home=MEMORY_DATA / "config",
        credentials=MEMORY_DATA / "config" / "credentials",
        project_file=MEMORY_DATA / "config" / "project.json",
        settings_env=MEMORY_SETTINGS,
        models=MEMORY_DATA / "config" / "models.json",
        routes=MEMORY_DATA / "config" / "routes.json",
        pricing=MEMORY_DATA / "config" / "pricing.json",
        state=MEMORY_DATA / "config" / "service-state.json",
        log=MEMORY_DATA / "config" / "memory-gateway.log",
    )
    result = restore_stack_backup(
        archive_path=ARCHIVE,
        paths=paths,
        memory_database=MEMORY_DATA / "memory.db",
        knowledge_database=MEMORY_DATA / "knowledge.db",
        auth_database=MEMORY_DATA / "auth.db",
        model_gateway_home=MODEL_DATA,
    )
    if not result.get("restored"):
        raise RuntimeError("backup contained no restorable components")
    _secure_tree(MEMORY_DATA, MEMORY_UID)
    _secure_tree(MEMORY_SETTINGS.parent, MEMORY_UID)
    _secure_tree(MODEL_DATA, MODEL_UID)
    print("离线恢复完成；密钥卷未进入归档且未被替换。")
    return 0


def _secure_tree(root: Path, owner: int) -> None:
    for current, directories, files in os.walk(root, followlinks=False):
        path = Path(current)
        if path.is_symlink():
            raise RuntimeError("restore destination contains a symlink")
        os.chown(path, owner, owner)
        path.chmod(0o700)
        for name in [*directories, *files]:
            child = path / name
            if child.is_symlink():
                raise RuntimeError("restore destination contains a symlink")
            os.chown(child, owner, owner)
            child.chmod(0o700 if child.is_dir() else 0o600)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"离线恢复失败：{type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
