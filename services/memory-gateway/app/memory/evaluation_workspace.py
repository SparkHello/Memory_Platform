"""Filesystem transaction primitives for per-user evaluation workspaces."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from typing import Iterator
from urllib.parse import quote
from uuid import uuid4

from app.memory.redaction import redact_memory_payload
from app.memory.store import ClosingSQLiteConnection, MemoryStore


SNAPSHOT_NAME = "eval_snapshot.db"
SNAPSHOT_PREFIX = "eval_snapshot_"
SNAPSHOT_POINTER_NAME = "current_snapshot.txt"
PREVIEW_NAME = "memories_preview.tsv"
LABELS_NAME = "labels.jsonl"
KEYWORD_RESULT_NAME = "last_keyword_result.json"
EMBEDDING_RESULT_NAME = "last_embedding_result.json"
USER_WORKSPACES_NAME = "users"
TRASH_NAME = ".trash"
TRASH_ROOT_MARKER_NAME = ".memory-platform-evaluation-trash-v1"
TRASH_ROOT_MARKER_VALUE = "memory-platform-evaluation-trash-v1\n"
TRASH_MANIFEST_NAME = "manifest.json"
TRASH_MANIFEST_TEMP_NAME = ".manifest.json.tmp"
TRASH_MANIFEST_VERSION = 1
WORKSPACE_LOCK_NAME = ".workspace.lock"


@dataclass(frozen=True, slots=True)
class StagedEvaluationWorkspace:
    eval_dir: Path
    trash_dir: Path | None
    moved: tuple[tuple[Path, Path], ...]
    workspace_removed: bool
    legacy_artifacts_removed: int

    def result(self, *, cleanup_failed: bool = False) -> dict[str, int | bool]:
        result: dict[str, int | bool] = {
            "workspace_removed": self.workspace_removed,
            "legacy_artifacts_removed": self.legacy_artifacts_removed,
        }
        if cleanup_failed:
            result["cleanup_failed"] = True
        return result


@contextmanager
def evaluation_workspace_lock(eval_dir: str | Path) -> Iterator[None]:
    """Serialize every evaluation workspace mutation across processes.

    The lock lives beside, rather than inside, a user workspace so staging a
    user's directory cannot remove the lock that protects the transaction.
    """

    eval_path = Path(eval_dir)
    lock_root = eval_path / USER_WORKSPACES_NAME
    if _is_link_or_junction(lock_root):
        raise OSError("evaluation workspace root must not be a link or junction")
    lock_root.mkdir(parents=True, exist_ok=True)
    if _is_link_or_junction(lock_root):
        raise OSError("evaluation workspace root must not be a link or junction")
    try:
        lock_root.resolve().relative_to(eval_path.resolve())
    except (OSError, ValueError) as exc:
        raise OSError("evaluation workspace root escapes EVAL_DIR") from exc
    lock_path = lock_root / WORKSPACE_LOCK_NAME
    if _is_link_or_junction(lock_path):
        raise OSError("evaluation workspace lock must not be a link or junction")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if (
                        getattr(exc, "winerror", None) not in {32, 33}
                        and getattr(exc, "errno", None) != 13
                    ):
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def user_eval_dir(eval_dir: str | Path, *, user_id: str) -> Path:
    normalized_user_id = user_id or "default"
    digest = hashlib.sha256(normalized_user_id.encode("utf-8")).hexdigest()
    return Path(eval_dir) / USER_WORKSPACES_NAME / digest


class EvaluationMemoryStore(MemoryStore):
    """Read-only store whose context-managed connections actually close."""

    def _connect(self) -> sqlite3.Connection:
        resolved = Path(self.database_path).resolve()
        uri_path = quote(resolved.as_posix(), safe="/:")
        connection = sqlite3.connect(
            f"file:{uri_path}?mode=ro",
            uri=True,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


def connect_readonly_database(database_path: Path) -> sqlite3.Connection:
    resolved = database_path.resolve()
    uri_path = quote(resolved.as_posix(), safe="/:")
    return sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)


def initialize_eval_workspace(
    *,
    source_db: str | Path,
    eval_dir: str | Path,
    user_id: str,
    labels_template: str,
    candidate_pool: int,
    is_locally_sensitive: Callable[[object], bool],
    filter_snapshot: Callable[..., None],
) -> dict[str, object]:
    """Publish a single-user snapshot and all derived workspace artifacts."""
    source_path = Path(source_db)
    if not source_path.exists():
        return {"error": f"Source database does not exist: {source_path}"}

    out_dir = user_eval_dir(eval_dir, user_id=user_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = new_snapshot_path(out_dir)
    preview_path = out_dir / PREVIEW_NAME
    labels_path = out_dir / LABELS_NAME

    snapshot_readonly(
        source_path,
        snapshot_path,
        eval_root=Path(eval_dir),
        user_id=user_id,
        filter_snapshot=filter_snapshot,
    )
    user_counts, preview_rows = read_snapshot_overview(
        snapshot_path,
        user_id=user_id,
        candidate_pool=candidate_pool,
        is_locally_sensitive=is_locally_sensitive,
    )
    write_preview(preview_path, preview_rows)
    write_current_snapshot_pointer(out_dir, snapshot_path)
    cleanup_old_snapshots(out_dir, current_snapshot=snapshot_path)
    invalidate_eval_results(out_dir)

    labels_created = False
    if not labels_path.exists():
        labels_path.write_text(labels_template, encoding="utf-8")
        labels_created = True

    return {
        "snapshot": str(snapshot_path),
        "preview": str(preview_path),
        "labels": str(labels_path),
        "labels_created": labels_created,
        "memory_count": len(preview_rows),
        "user_counts": user_counts,
        "user_id": user_id,
    }


def require_eval_workspace(
    eval_dir: str | Path,
    *,
    user_id: str,
) -> tuple[Path, Path, Path]:
    eval_path, snapshot_path, labels_path = eval_workspace_paths(
        eval_dir,
        user_id=user_id,
    )
    if not snapshot_path.exists():
        raise FileNotFoundError(
            f"Snapshot not found: {snapshot_path}. Run recall init first."
        )
    if not labels_path.exists():
        raise FileNotFoundError(
            f"Labels not found: {labels_path}. Run recall init first."
        )
    return eval_path, snapshot_path, labels_path


def eval_workspace_paths(
    eval_dir: str | Path,
    *,
    user_id: str,
) -> tuple[Path, Path, Path]:
    eval_path = user_eval_dir(eval_dir, user_id=user_id)
    return (
        eval_path,
        current_snapshot_path(eval_path),
        eval_path / LABELS_NAME,
    )


def load_labels_file(
    labels_path: str | Path,
    *,
    normalize_entry: Callable[..., dict[str, object]],
    error_type: type[ValueError],
) -> list[dict[str, object]]:
    path = Path(labels_path)
    labels: list[dict[str, object]] = []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise error_type(
            f"Labels file is not valid UTF-8: {path}: {exc}"
        ) from exc
    for index, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise error_type(f"Invalid label JSON on line {index}: {exc}") from exc
        try:
            labels.append(normalize_entry(entry, index=index))
        except ValueError as exc:
            raise error_type(f"Invalid label on line {index}: {exc}") from exc
    return labels


def write_labels_atomic(
    labels_path: Path,
    labels: list[dict[str, object]],
) -> None:
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(label, ensure_ascii=False, sort_keys=True)
        for label in labels
    ]
    tmp_path = labels_path.with_suffix(labels_path.suffix + ".tmp")
    tmp_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    tmp_path.replace(labels_path)


def save_eval_result_file(
    eval_dir: str | Path,
    *,
    result_name: str,
    result: dict[str, object],
) -> Path:
    path = Path(eval_dir) / result_name
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, result)
    return path


def load_eval_results(
    eval_dir: str | Path,
    *,
    result_names: dict[str, str],
    snapshot_path: str | Path | None = None,
) -> dict[str, object]:
    eval_path = Path(eval_dir)
    expected_snapshot = str(Path(snapshot_path)) if snapshot_path is not None else None
    results: dict[str, object] = {}
    for mode, result_name in result_names.items():
        path = eval_path / result_name
        if not path.exists():
            results[mode] = None
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            results[mode] = None
            continue
        if not isinstance(result, dict) or (
            expected_snapshot is not None and result.get("snapshot") != expected_snapshot
        ):
            results[mode] = None
            continue
        results[mode] = result
    return results


def new_snapshot_path(eval_dir: Path) -> Path:
    while True:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        path = eval_dir / f"{SNAPSHOT_PREFIX}{stamp}.db"
        if not path.exists():
            return path


def current_snapshot_path(eval_dir: str | Path) -> Path:
    eval_path = Path(eval_dir)
    pointer_path = eval_path / SNAPSHOT_POINTER_NAME
    try:
        pointed_name = pointer_path.read_text(encoding="utf-8").strip()
    except OSError:
        pointed_name = ""

    if pointed_name:
        pointed_path = eval_path / pointed_name
        if pointed_path.exists():
            return pointed_path

    legacy_path = eval_path / SNAPSHOT_NAME
    if legacy_path.exists():
        return legacy_path

    snapshots = sorted(
        eval_path.glob(f"{SNAPSHOT_PREFIX}*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return snapshots[0] if snapshots else legacy_path


def write_current_snapshot_pointer(eval_dir: Path, snapshot_path: Path) -> None:
    pointer_path = eval_dir / SNAPSHOT_POINTER_NAME
    tmp_path = pointer_path.with_name(pointer_path.name + ".tmp")
    tmp_path.write_text(snapshot_path.name, encoding="utf-8")
    tmp_path.replace(pointer_path)


def cleanup_old_snapshots(
    eval_dir: Path,
    *,
    current_snapshot: Path,
    keep: int = 3,
) -> None:
    snapshots = [
        path
        for path in eval_dir.glob(f"{SNAPSHOT_PREFIX}*.db")
        if path.resolve() != current_snapshot.resolve()
    ]
    legacy_path = eval_dir / SNAPSHOT_NAME
    if legacy_path.exists() and legacy_path.resolve() != current_snapshot.resolve():
        snapshots.append(legacy_path)

    snapshots.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for snapshot_path in snapshots[keep:]:
        unlink_sqlite_database(snapshot_path)


def invalidate_eval_results(eval_dir: Path) -> None:
    for name in (KEYWORD_RESULT_NAME, EMBEDDING_RESULT_NAME):
        (eval_dir / name).unlink(missing_ok=True)


def unlink_sqlite_database(
    path: Path,
    *,
    ignore_permission_error: bool = True,
) -> int:
    removed = 0
    for target in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ):
        try:
            if target.is_file():
                target.unlink()
                removed += 1
        except PermissionError:
            if ignore_permission_error:
                continue
            raise
    return removed


def snapshot_readonly(
    source_path: Path,
    snapshot_path: Path,
    *,
    eval_root: Path,
    user_id: str,
    filter_snapshot: Callable[..., None],
) -> None:
    """Use SQLite backup and atomically publish a filtered single-user copy.

    SQLite backup first produces a full database copy. Keep that unfiltered
    intermediate only in a marked, globally recoverable transaction directory;
    a hard crash must never strand it in any user's published workspace.
    """
    resolved = source_path.resolve()
    uri_path = quote(resolved.as_posix(), safe="/:")
    transaction_dir = _create_managed_transaction(
        eval_root,
        kind="snapshot-build",
        user_id=user_id,
        target_memory_ids=(),
        mappings=(),
    )
    temp_path = transaction_dir / "snapshot.db"
    unlink_sqlite_database(temp_path)
    source = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(temp_path))
        try:
            source.backup(dest)
            dest.execute("PRAGMA journal_mode = DELETE")
            filter_snapshot(dest, user_id=user_id)
        finally:
            dest.close()
        for sidecar in (
            Path(str(temp_path) + "-wal"),
            Path(str(temp_path) + "-shm"),
            Path(str(temp_path) + "-journal"),
        ):
            sidecar.unlink(missing_ok=True)
        temp_path.replace(snapshot_path)
    except Exception:
        unlink_sqlite_database(temp_path)
        raise
    finally:
        source.close()
        if not temp_path.exists():
            _remove_managed_transaction(transaction_dir)
            _remove_empty_trash_root(eval_root)


def filter_snapshot_to_user(
    connection: sqlite3.Connection,
    *,
    user_id: str,
) -> None:
    connection.execute("PRAGMA secure_delete = ON")
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for row in table_rows:
        table_name = str(row[0])
        quoted_table = quote_identifier(table_name)
        columns = {
            str(column[1])
            for column in connection.execute(
                f"PRAGMA table_info({quoted_table})"
            ).fetchall()
        }
        if "user_id" in columns:
            connection.execute(
                f"DELETE FROM {quoted_table} "
                "WHERE COALESCE(user_id, 'default') <> ?",
                (user_id,),
            )
            connection.execute(
                f"UPDATE {quoted_table} "
                "SET user_id = 'default' WHERE user_id IS NULL"
            )
        else:
            connection.execute(f"DELETE FROM {quoted_table}")
    connection.commit()
    connection.execute("VACUUM")


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def read_snapshot_overview(
    snapshot_path: Path,
    *,
    user_id: str,
    candidate_pool: int,
    is_locally_sensitive: Callable[[object], bool],
) -> tuple[dict[str, int], list[tuple[str, str, str]]]:
    connection = sqlite3.connect(str(snapshot_path))
    try:
        connection.row_factory = sqlite3.Row
        user_rows = connection.execute(
            "SELECT COALESCE(user_id, 'default') AS user_id, COUNT(*) AS count "
            "FROM memories WHERE COALESCE(archived, 0) = 0 "
            "GROUP BY user_id ORDER BY count DESC"
        ).fetchall()
        user_counts = {
            str(row["user_id"]): int(row["count"])
            for row in user_rows
        }
    finally:
        connection.close()
    memories = eligible_snapshot_memories(
        snapshot_path,
        user_id=user_id,
        candidate_pool=candidate_pool,
        is_locally_sensitive=is_locally_sensitive,
    )
    preview = [
        (memory.id, memory.type, one_line(memory.content))
        for memory in memories
    ]
    return user_counts, preview


def write_preview(
    preview_path: Path,
    rows: list[tuple[str, str, str]],
) -> None:
    lines = ["id\ttype\tcontent_preview"]
    lines.extend(
        f"{memory_id}\t{memory_type}\t{content}"
        for memory_id, memory_type, content in rows
    )
    preview_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def snapshot_memories(
    snapshot_path: Path,
    *,
    user_id: str,
    redact_sensitive: bool,
    candidate_pool: int,
    is_locally_sensitive: Callable[[object], bool],
) -> list[dict[str, object]]:
    memories = eligible_snapshot_memories(
        snapshot_path,
        user_id=user_id,
        candidate_pool=candidate_pool,
        is_locally_sensitive=is_locally_sensitive,
    )
    payloads: list[dict[str, object]] = []
    for memory in memories:
        payload = memory.model_dump(exclude={"embedding_json"})
        payloads.append(
            redact_memory_payload(payload, redact_sensitive=redact_sensitive)
        )
    return payloads


def eligible_snapshot_memories(
    snapshot_path: Path,
    *,
    user_id: str,
    candidate_pool: int,
    is_locally_sensitive: Callable[[object], bool],
):
    """Mirror the default search candidate pool before scoring."""
    store = EvaluationMemoryStore(str(snapshot_path))
    memories = store.list_memories(
        user_id=user_id,
        limit=candidate_pool,
        include_lifecycle_archived=False,
    )
    return [
        memory
        for memory in memories
        if memory.origin == "user_asserted"
        and not is_locally_sensitive(memory)
    ]


def snapshot_memory_ids(
    snapshot_path: Path,
    *,
    user_id: str,
    candidate_pool: int,
    is_locally_sensitive: Callable[[object], bool],
) -> set[str]:
    if not snapshot_path.exists():
        raise FileNotFoundError(
            f"Snapshot not found: {snapshot_path}. Run recall init first."
        )
    return {
        str(memory["id"])
        for memory in snapshot_memories(
            snapshot_path,
            user_id=user_id,
            redact_sensitive=False,
            candidate_pool=candidate_pool,
            is_locally_sensitive=is_locally_sensitive,
        )
    }


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def one_line(text: str, limit: int = 120) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def stage_user_eval_workspace(
    eval_dir: str | Path,
    *,
    user_id: str,
    target_memory_ids: list[str] | tuple[str, ...] = (),
    database_path: str | Path | None = None,
    committed_intent: bool = False,
) -> StagedEvaluationWorkspace:
    """Atomically move purge-sensitive evaluation files into local trash.

    Every individual rename stays on the same filesystem. If any rename fails,
    already moved entries are restored before the exception escapes, so callers
    can guarantee that a database purge never starts after partial staging.
    """
    eval_path = Path(eval_dir)
    # The caller holds the global workspace lock. Resolve every prior managed
    # transaction before starting a new purge. An invalid or undecidable entry
    # fails closed so an older snapshot cannot retain the new purge target.
    prepare_evaluation_workspace_mutation(
        eval_path,
        database_path=database_path,
    )
    user_path = user_eval_dir(eval_path, user_id=user_id)
    workspace_removed = user_path.exists()
    legacy_paths = _legacy_artifact_paths(eval_path)
    originals = ([user_path] if workspace_removed else []) + legacy_paths
    if not originals:
        return StagedEvaluationWorkspace(
            eval_dir=eval_path,
            trash_dir=None,
            moved=(),
            workspace_removed=False,
            legacy_artifacts_removed=0,
        )
    resolved_eval_path = eval_path.resolve()
    for path in originals:
        if _is_link_or_junction(path):
            raise OSError(
                "evaluation workspace purge refuses linked or junction artifacts"
            )
        try:
            path.resolve().relative_to(resolved_eval_path)
        except (OSError, ValueError) as exc:
            raise OSError("evaluation purge artifact escapes EVAL_DIR") from exc

    mappings = tuple(
        (str(original.relative_to(eval_path)), str(index))
        for index, original in enumerate(originals)
    )
    trash_dir = _create_managed_transaction(
        eval_path,
        kind="purge",
        phase="committed" if committed_intent else "staged",
        user_id=user_id,
        target_memory_ids=tuple(sorted(set(target_memory_ids))),
        mappings=mappings,
    )
    moved: list[tuple[Path, Path]] = []
    try:
        # The in-memory mapping already retains every original path. Keep the
        # staged layout flat so a user hash plus snapshot filename does not
        # cross the traditional Windows MAX_PATH boundary after staging.
        for index, original in enumerate(originals):
            destination = trash_dir / str(index)
            original.replace(destination)
            moved.append((original, destination))
            _fsync_directory(original.parent)
            _fsync_directory(trash_dir)
    except Exception:
        _restore_entries(moved)
        _remove_managed_transaction(trash_dir)
        _remove_empty_trash_root(eval_path)
        raise
    return StagedEvaluationWorkspace(
        eval_dir=eval_path,
        trash_dir=trash_dir,
        moved=tuple(moved),
        workspace_removed=workspace_removed,
        legacy_artifacts_removed=len(legacy_paths),
    )


def restore_staged_eval_workspace(staged: StagedEvaluationWorkspace) -> None:
    """Restore a staged workspace after the database transaction failed."""
    _restore_entries(list(staged.moved))
    if staged.trash_dir is not None:
        _remove_managed_transaction(staged.trash_dir)
        _remove_empty_trash_root(staged.eval_dir)


def mark_staged_eval_workspace_committed(
    staged: StagedEvaluationWorkspace,
) -> None:
    """Durably record the database commit before deleting staged files."""

    if staged.trash_dir is None:
        return
    manifest = _read_transaction_manifest(staged.trash_dir)
    if manifest.get("kind") != "purge":
        raise OSError("evaluation purge transaction manifest has invalid kind")
    manifest["phase"] = "committed"
    _write_transaction_manifest(staged.trash_dir, manifest)


def discard_staged_eval_workspace(
    staged: StagedEvaluationWorkspace,
) -> dict[str, int | bool]:
    """Delete staged data after the database transaction has committed."""
    if staged.trash_dir is None:
        return staged.result()
    try:
        _remove_managed_transaction(staged.trash_dir)
        _remove_empty_trash_root(staged.eval_dir)
    except OSError:
        return staged.result(cleanup_failed=True)
    return staged.result()


def cleanup_abandoned_eval_trash(
    eval_dir: str | Path,
    *,
    database_path: str | Path | None = None,
) -> int:
    """Recover only marked Memory Platform evaluation transactions.

    A staged purge is restored when its target rows still exist (the SQLite
    transaction rolled back), and discarded when all targets are gone. A
    caller without a database cannot safely decide and leaves staged purges in
    place. Snapshot-build transactions never contain manual labels and are
    always discarded.
    """

    eval_path = Path(eval_dir)
    if _safe_trash_root(eval_path, create=False) is None:
        return 0
    with evaluation_workspace_lock(eval_path):
        return _cleanup_abandoned_eval_trash_locked(
            eval_path,
            database_path=Path(database_path) if database_path is not None else None,
        )


def prepare_evaluation_workspace_mutation(
    eval_dir: str | Path,
    *,
    database_path: str | Path | None,
) -> int:
    """Resolve prior transactions before a caller mutates a locked workspace."""

    return _cleanup_abandoned_eval_trash_locked(
        Path(eval_dir),
        database_path=Path(database_path) if database_path is not None else None,
        fail_on_unresolved=True,
    )


def delete_user_eval_workspace(
    eval_dir: str | Path,
    *,
    user_id: str,
) -> dict[str, int | bool]:
    """Compatibility helper for non-transactional administrative cleanup."""
    with evaluation_workspace_lock(eval_dir):
        staged = stage_user_eval_workspace(
            eval_dir,
            user_id=user_id,
            committed_intent=True,
        )
        result = discard_staged_eval_workspace(staged)
        if result.get("cleanup_failed"):
            raise OSError("evaluation trash cleanup failed")
        return result


def _legacy_artifact_paths(eval_path: Path) -> list[Path]:
    candidates = {
        eval_path / SNAPSHOT_POINTER_NAME,
        eval_path / PREVIEW_NAME,
        eval_path / LABELS_NAME,
        eval_path / KEYWORD_RESULT_NAME,
        eval_path / EMBEDDING_RESULT_NAME,
        eval_path / SNAPSHOT_NAME,
        Path(str(eval_path / SNAPSHOT_NAME) + "-wal"),
        Path(str(eval_path / SNAPSHOT_NAME) + "-shm"),
        Path(str(eval_path / SNAPSHOT_NAME) + "-journal"),
    }
    candidates.update(eval_path.glob(f"{SNAPSHOT_PREFIX}*.db*"))
    return sorted(
        (path for path in candidates if path.exists()),
        key=lambda path: path.name,
    )


def _restore_entries(moved: list[tuple[Path, Path]]) -> None:
    for original, staged in reversed(moved):
        if not staged.exists():
            continue
        if original.exists():
            raise FileExistsError(
                f"cannot restore evaluation workspace over {original.name}"
            )
        original.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(original)
        _fsync_directory(staged.parent)
        _fsync_directory(original.parent)


def _remove_empty_trash_root(eval_dir: Path) -> None:
    # Keep an empty owned root and its marker. On Windows, unlinking the marker
    # before rmdir creates a permanent fail-closed state if AV/indexing holds a
    # transient directory handle. The tiny marker is the safer stable state.
    _safe_trash_root(eval_dir, create=False)


def _safe_trash_root(eval_dir: Path, *, create: bool) -> Path | None:
    trash_root = eval_dir / TRASH_NAME
    if _is_link_or_junction(trash_root):
        raise OSError("evaluation trash root must not be a link or junction")
    if trash_root.exists():
        if not trash_root.is_dir():
            raise OSError("evaluation trash root must be a directory")
        _validate_trash_root_marker(trash_root)
        return trash_root
    if not create:
        return None
    trash_root.mkdir(parents=True, exist_ok=True)
    if _is_link_or_junction(trash_root) or not trash_root.is_dir():
        raise OSError("evaluation trash root is unsafe")
    marker = trash_root / TRASH_ROOT_MARKER_NAME
    try:
        _write_text_durable(marker, TRASH_ROOT_MARKER_VALUE)
        _fsync_directory(trash_root)
    except Exception:
        try:
            trash_root.rmdir()
        except OSError:
            pass
        raise
    return trash_root


def _validate_trash_root_marker(trash_root: Path) -> None:
    marker = trash_root / TRASH_ROOT_MARKER_NAME
    try:
        valid = (
            marker.is_file()
            and not _is_link_or_junction(marker)
            and marker.read_text(encoding="utf-8") == TRASH_ROOT_MARKER_VALUE
        )
    except (OSError, UnicodeError):
        valid = False
    if not valid:
        raise OSError(
            "existing evaluation .trash is not owned by Memory Platform"
        )


def _create_managed_transaction(
    eval_dir: Path,
    *,
    kind: str,
    phase: str | None = None,
    user_id: str,
    target_memory_ids: tuple[str, ...],
    mappings: tuple[tuple[str, str], ...],
) -> Path:
    if kind not in {"purge", "snapshot-build"}:
        raise ValueError("unsupported evaluation transaction kind")
    effective_phase = phase or ("staged" if kind == "purge" else "building")
    if effective_phase not in (
        {"staged", "committed"} if kind == "purge" else {"building"}
    ):
        raise ValueError("unsupported evaluation transaction phase")
    trash_root = _safe_trash_root(eval_dir, create=True)
    assert trash_root is not None
    purge_id = uuid4().hex
    transaction_dir = trash_root / purge_id
    transaction_dir.mkdir(parents=False, exist_ok=False)
    manifest: dict[str, object] = {
        "schema_version": TRASH_MANIFEST_VERSION,
        "owner": "memory-platform-evaluation",
        "purge_id": purge_id,
        "kind": kind,
        "phase": effective_phase,
        "user_id": user_id,
        "target_memory_ids": list(target_memory_ids),
        "mappings": [
            {"original": original, "slot": slot}
            for original, slot in mappings
        ],
    }
    try:
        _write_transaction_manifest(transaction_dir, manifest)
    except Exception:
        try:
            transaction_dir.rmdir()
        except OSError:
            pass
        raise
    return transaction_dir


def _write_transaction_manifest(
    transaction_dir: Path,
    manifest: dict[str, object],
) -> None:
    _validate_transaction_directory_name(transaction_dir)
    manifest_path = transaction_dir / TRASH_MANIFEST_NAME
    temp_path = transaction_dir / TRASH_MANIFEST_TEMP_NAME
    serialized = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    if temp_path.exists():
        if not temp_path.is_file() or _is_link_or_junction(temp_path):
            raise OSError("evaluation manifest temporary path is unsafe")
        temp_path.unlink()
    _write_text_durable(temp_path, serialized)
    temp_path.replace(manifest_path)
    _fsync_directory(transaction_dir)


def _write_text_durable(path: Path, value: str) -> None:
    if _is_link_or_junction(path):
        raise OSError("refusing to replace a linked evaluation metadata file")
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_transaction_manifest(transaction_dir: Path) -> dict[str, object]:
    _validate_transaction_directory_name(transaction_dir)
    manifest_path = transaction_dir / TRASH_MANIFEST_NAME
    if not manifest_path.is_file() or _is_link_or_junction(manifest_path):
        raise OSError("evaluation transaction manifest is missing or unsafe")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OSError("evaluation transaction manifest is invalid") from exc
    if not isinstance(raw, dict):
        raise OSError("evaluation transaction manifest is invalid")
    if (
        raw.get("schema_version") != TRASH_MANIFEST_VERSION
        or raw.get("owner") != "memory-platform-evaluation"
        or raw.get("purge_id") != transaction_dir.name
        or raw.get("kind") not in {"purge", "snapshot-build"}
    ):
        raise OSError("evaluation transaction manifest identity is invalid")
    kind = str(raw["kind"])
    expected_phases = {"purge": {"staged", "committed"}, "snapshot-build": {"building"}}
    if raw.get("phase") not in expected_phases[kind]:
        raise OSError("evaluation transaction phase is invalid")
    user_id = raw.get("user_id")
    targets = raw.get("target_memory_ids")
    mappings = raw.get("mappings")
    if not isinstance(user_id, str) or not isinstance(targets, list) or not isinstance(mappings, list):
        raise OSError("evaluation transaction fields are invalid")
    if not all(isinstance(item, str) and item for item in targets):
        raise OSError("evaluation transaction target IDs are invalid")
    _validated_manifest_mappings(mappings)
    _validate_transaction_entries(transaction_dir, raw)
    return raw


def _validated_manifest_mappings(
    raw_mappings: list[object],
) -> tuple[tuple[Path, str], ...]:
    validated: list[tuple[Path, str]] = []
    slots: set[str] = set()
    originals: set[str] = set()
    for item in raw_mappings:
        if not isinstance(item, dict):
            raise OSError("evaluation transaction mapping is invalid")
        original = item.get("original")
        slot = item.get("slot")
        if not isinstance(original, str) or not isinstance(slot, str):
            raise OSError("evaluation transaction mapping is invalid")
        relative = Path(original)
        if (
            relative.is_absolute()
            or bool(relative.drive or relative.root or relative.anchor)
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or not re.fullmatch(r"\d+", slot)
            or slot in slots
            or original in originals
        ):
            raise OSError("evaluation transaction mapping escapes its workspace")
        slots.add(slot)
        originals.add(original)
        validated.append((relative, slot))
    return tuple(validated)


def _validate_transaction_directory_name(transaction_dir: Path) -> None:
    if (
        not re.fullmatch(r"[0-9a-f]{32}", transaction_dir.name)
        or not transaction_dir.is_dir()
        or _is_link_or_junction(transaction_dir)
    ):
        raise OSError("evaluation transaction directory is unsafe")


def _validate_transaction_entries(
    transaction_dir: Path,
    manifest: dict[str, object],
) -> None:
    allowed = {TRASH_MANIFEST_NAME, TRASH_MANIFEST_TEMP_NAME}
    if manifest.get("kind") == "snapshot-build":
        allowed.update(
            {
                "snapshot.db",
                "snapshot.db-wal",
                "snapshot.db-shm",
                "snapshot.db-journal",
            }
        )
    else:
        mappings = _validated_manifest_mappings(list(manifest.get("mappings", [])))
        allowed.update(slot for _, slot in mappings)
    unknown = sorted(path.name for path in transaction_dir.iterdir() if path.name not in allowed)
    if unknown:
        raise OSError(
            "evaluation transaction contains unknown entries: " + ", ".join(unknown)
        )


def _remove_managed_transaction(transaction_dir: Path) -> None:
    if not transaction_dir.exists():
        return
    manifest = _read_transaction_manifest(transaction_dir)
    # The manifest is the durable recovery authority, so remove it last. If a
    # process dies during recursive slot cleanup, startup can validate the same
    # transaction and resume without stranding unowned sensitive files. The
    # only state allowed after manifest removal is an empty UUID directory;
    # cleanup recognizes that narrow tombstone so a failed final rmdir can be
    # retried safely.
    if manifest.get("kind") == "purge":
        mappings = _validated_manifest_mappings(list(manifest["mappings"]))
        for _, slot in mappings:
            _remove_transaction_payload(transaction_dir / slot)
    else:
        for name in (
            "snapshot.db",
            "snapshot.db-wal",
            "snapshot.db-shm",
            "snapshot.db-journal",
        ):
            _remove_transaction_payload(transaction_dir / name)
    (transaction_dir / TRASH_MANIFEST_TEMP_NAME).unlink(missing_ok=True)
    (transaction_dir / TRASH_MANIFEST_NAME).unlink()
    transaction_dir.rmdir()
    _fsync_directory(transaction_dir.parent)


def _remove_transaction_payload(path: Path) -> None:
    if not path.exists() and not _is_link_or_junction(path):
        return
    if _is_link_or_junction(path):
        raise OSError("evaluation transaction payload became a link or junction")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _cleanup_abandoned_eval_trash_locked(
    eval_dir: Path,
    *,
    database_path: Path | None,
    fail_on_unresolved: bool = False,
) -> int:
    trash_root = _safe_trash_root(eval_dir, create=False)
    if trash_root is None:
        return 0
    processed = 0
    unknown: list[str] = []
    unresolved: list[str] = []
    for transaction_dir in list(trash_root.iterdir()):
        if transaction_dir.name == TRASH_ROOT_MARKER_NAME:
            continue
        try:
            manifest = _read_transaction_manifest(transaction_dir)
        except OSError:
            if _remove_empty_transaction_tombstone(transaction_dir):
                processed += 1
                continue
            unknown.append(transaction_dir.name)
            continue
        kind = str(manifest["kind"])
        phase = str(manifest["phase"])
        if kind == "snapshot-build":
            _remove_managed_transaction(transaction_dir)
            processed += 1
            continue
        if phase == "committed":
            _discard_committed_purge_transaction(
                eval_dir,
                transaction_dir,
                manifest,
            )
            processed += 1
            continue
        targets = [str(item) for item in manifest["target_memory_ids"]]
        targets_exist = _transaction_targets_exist(
            database_path,
            user_id=str(manifest["user_id"]),
            target_memory_ids=targets,
        )
        if targets_exist is None:
            unresolved.append(transaction_dir.name)
            continue
        if targets_exist:
            _restore_transaction_from_manifest(eval_dir, transaction_dir, manifest)
        else:
            _remove_managed_transaction(transaction_dir)
        processed += 1
    _remove_empty_trash_root(eval_dir)
    if unknown:
        raise OSError(
            "evaluation .trash contains unowned or invalid entries; preserved: "
            + ", ".join(sorted(unknown))
        )
    if fail_on_unresolved and unresolved:
        raise OSError(
            "evaluation .trash contains transactions whose database state "
            "cannot be determined; refusing a new mutation: "
            + ", ".join(sorted(unresolved))
        )
    return processed


def _remove_empty_transaction_tombstone(transaction_dir: Path) -> bool:
    """Retry the sole safe manifest-less terminal state.

    A transaction removes its manifest only after every managed payload. A
    crash or transient Windows directory handle can therefore leave an empty
    UUID directory. Anything non-empty remains unknown and is preserved.
    """

    if (
        not re.fullmatch(r"[0-9a-f]{32}", transaction_dir.name)
        or not transaction_dir.is_dir()
        or _is_link_or_junction(transaction_dir)
    ):
        return False
    try:
        next(transaction_dir.iterdir())
    except StopIteration:
        transaction_dir.rmdir()
        _fsync_directory(transaction_dir.parent)
        return True
    except OSError:
        return False
    return False


def _discard_committed_purge_transaction(
    eval_dir: Path,
    transaction_dir: Path,
    manifest: dict[str, object],
) -> None:
    """Finish a durable purge intent, including not-yet-staged originals."""

    for original, _ in _resolved_manifest_mappings(eval_dir, manifest):
        _remove_transaction_payload(original)
        _fsync_directory(original.parent)
    _remove_managed_transaction(transaction_dir)


def _restore_transaction_from_manifest(
    eval_dir: Path,
    transaction_dir: Path,
    manifest: dict[str, object],
) -> None:
    moved: list[tuple[Path, Path]] = []
    for original, slot in _resolved_manifest_mappings(eval_dir, manifest):
        moved.append((original, transaction_dir / slot))
    _restore_entries(moved)
    _remove_managed_transaction(transaction_dir)


def _resolved_manifest_mappings(
    eval_dir: Path,
    manifest: dict[str, object],
) -> tuple[tuple[Path, str], ...]:
    mappings = _validated_manifest_mappings(list(manifest["mappings"]))
    resolved_eval_dir = eval_dir.resolve()
    resolved: list[tuple[Path, str]] = []
    for relative, slot in mappings:
        if relative.parts[0] == TRASH_NAME:
            raise OSError("evaluation transaction original targets its trash root")
        original = eval_dir / relative
        if _is_link_or_junction(original):
            raise OSError("evaluation transaction original became a link or junction")
        try:
            original.resolve().relative_to(resolved_eval_dir)
        except (OSError, ValueError) as exc:
            raise OSError(
                "evaluation transaction original escapes its workspace"
            ) from exc
        resolved.append((original, slot))
    return tuple(resolved)


def _transaction_targets_exist(
    database_path: Path | None,
    *,
    user_id: str,
    target_memory_ids: list[str],
) -> bool | None:
    if database_path is None or not target_memory_ids or not database_path.exists():
        return None
    resolved = database_path.resolve()
    uri_path = quote(resolved.as_posix(), safe="/:")
    try:
        connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
        try:
            for offset in range(0, len(target_memory_ids), 500):
                batch = target_memory_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                row = connection.execute(
                    "SELECT 1 FROM memories WHERE user_id = ? "
                    f"AND id IN ({placeholders}) LIMIT 1",
                    (user_id, *batch),
                ).fetchone()
                if row is not None:
                    return True
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
    return False


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())
