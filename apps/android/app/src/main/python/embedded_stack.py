"""Memory Platform embedded runtime.

Runs Model Gateway and Memory Gateway inside one Python process, bound to the
loopback interface, without the ``memgw``/``modelgw`` CLIs, without spawning
subprocesses and without uvicorn's native extras (uvloop, httptools, websockets,
watchfiles). It exists for hosts where a normal service install is impossible:
Android (Chaquopy foreground service) and Termux. It also runs on a desktop for
verification::

    python -m embedded_stack --data-dir ~/memory-platform \
        --ui-dist services/memory-gateway/ui/dist

Layout under ``--data-dir``::

    memory-gateway/   settings.env, project.json, memory.db, knowledge.db, auth.db, eval/
    model-gateway/    config.json, secrets.env, usage.db, ...
    credentials/      gateway.txt (first console token), admin.txt (Model Gateway admin key)

Both credential files are written once with mode 0600. The host app shows them
to the user for the first Web console login; the service never prints them.

Kotlin entry points (all return JSON strings, all safe to call from any thread):
``start(data_dir, memory_port, model_port, ui_dist_dir)``, ``stop()``,
``status()``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
DEFAULT_MEMORY_PORT = 2026
DEFAULT_MODEL_PORT = 2030
_START_TIMEOUT_SECONDS = 60.0

logger = logging.getLogger("embedded_stack")


@dataclass(frozen=True)
class StackLayout:
    data_dir: Path
    memory_home: Path
    model_home: Path
    settings_env: Path
    credentials_dir: Path
    console_token_path: Path
    admin_key_path: Path
    memory_db: Path
    knowledge_db: Path
    auth_db: Path
    eval_dir: Path
    ui_dist_dir: Path | None
    memory_port: int
    model_port: int

    @property
    def memory_url(self) -> str:
        return f"http://{HOST}:{self.memory_port}"

    @property
    def model_url(self) -> str:
        return f"http://{HOST}:{self.model_port}"


@dataclass(frozen=True)
class BootstrapResult:
    console_token_created: bool
    admin_key_created: bool
    backend_client_created: bool
    console_token_path: str
    admin_key_path: str


def configure(
    data_dir: str | os.PathLike[str],
    *,
    memory_port: int = DEFAULT_MEMORY_PORT,
    model_port: int = DEFAULT_MODEL_PORT,
    ui_dist_dir: str | os.PathLike[str] | None = None,
) -> StackLayout:
    """Resolve the on-disk layout and point both services at it via env vars.

    Must run before ``app.*`` or ``model_gateway.*`` are imported: Memory
    Gateway reads ``MEMGW_HOME``/``MEMGW_SETTINGS_PATH`` and Model Gateway
    reads ``MODEL_GATEWAY_HOME`` when their modules initialise.
    """

    if memory_port == model_port:
        raise ValueError("memory_port 和 model_port 不能相同")
    root = Path(data_dir).expanduser().resolve()
    memory_home = root / "memory-gateway"
    model_home = root / "model-gateway"
    credentials = root / "credentials"
    ui = Path(ui_dist_dir).expanduser().resolve() if ui_dist_dir else None
    layout = StackLayout(
        data_dir=root,
        memory_home=memory_home,
        model_home=model_home,
        settings_env=memory_home / "settings.env",
        credentials_dir=credentials,
        console_token_path=credentials / "gateway.txt",
        admin_key_path=credentials / "admin.txt",
        memory_db=memory_home / "memory.db",
        knowledge_db=memory_home / "knowledge.db",
        auth_db=memory_home / "auth.db",
        eval_dir=memory_home / "eval",
        ui_dist_dir=ui,
        memory_port=memory_port,
        model_port=model_port,
    )
    for directory in (root, memory_home, model_home, credentials, layout.eval_dir):
        directory.mkdir(parents=True, exist_ok=True)
        _chmod(directory, 0o700)
    os.environ["MEMGW_HOME"] = str(memory_home)
    os.environ["MEMGW_SETTINGS_PATH"] = str(layout.settings_env)
    os.environ["MODEL_GATEWAY_HOME"] = str(model_home)
    os.environ.pop("MODEL_GATEWAY_SECRETS_PATH", None)
    # Phones routinely sit behind Clash/Surge-style VPNs whose fake-ip DNS maps
    # every hostname into 198.18.0.0/15. The device owner is the only tenant
    # here, so accept that range instead of failing every upstream call.
    os.environ.setdefault("MODEL_GATEWAY_ALLOW_FAKE_IP", "1")
    # First-run credentials are generated into 0600 files only; never inherit
    # them from the process environment.
    for name in ("GATEWAY_API_KEY", "GATEWAY_SIGNING_SECRET", "MODEL_GATEWAY_API_KEY"):
        os.environ.pop(name, None)
    # Relative defaults inside the services resolve against the working
    # directory, which on Android is "/" and read-only.
    os.chdir(root)
    return layout


def bootstrap(layout: StackLayout) -> BootstrapResult:
    """Idempotently wire Model Gateway <-> Memory Gateway and mint first credentials.

    Mirrors ``app.stack_install.apply_stack_install`` for a single-process
    loopback deployment, but drives Model Gateway through its control-plane
    API instead of the ``modelgw`` subprocess, which does not exist on Android.
    """

    from app.auth.tokens import AuthTokenStore
    from app.cli_config import cli_paths, ensure_initialized, update_env_value
    from model_gateway.config_store import (
        gateway_paths,
        initialize,
        load_config,
        read_secrets,
    )
    from model_gateway.control_plane import ControlPlaneService
    from model_gateway.ids import default_secret_ref
    from model_gateway.memory_client import provision_memory_gateway_client
    from model_gateway_contracts.models import ClientConfig

    # --- Model Gateway: backend client for Memory Gateway + admin client -----
    paths = gateway_paths(layout.model_home)
    initialize(paths)
    config = load_config(paths.config)
    secret_values = read_secrets(paths.secrets)

    backend = provision_memory_gateway_client(config, secret_values)
    clients: dict[str, ClientConfig] = {}
    secret_updates: dict[str, str] = {}
    if backend.created:
        clients["memory-gateway"] = backend.client
    if secret_values.get(backend.client.secret_ref) != backend.key:
        secret_updates[backend.client.secret_ref] = backend.key

    admin_id = "memory-console-admin"
    admin = config.clients.get(admin_id)
    admin_ref = admin.secret_ref if admin is not None else default_secret_ref("CLIENT", admin_id)
    if admin is None or admin.kind != "admin" or not admin.enabled:
        clients[admin_id] = ClientConfig(
            kind="admin",
            secret_ref=admin_ref,
            allowed_routes=["*"],
            allow_direct_deployments=False,
        )
    admin_key_created = False
    if not secret_values.get(admin_ref) or not layout.admin_key_path.exists():
        # Either never provisioned, or the user-visible copy is gone. Rotate so
        # the app can always show a key that actually works.
        admin_key = secrets.token_urlsafe(48)
        secret_updates[admin_ref] = admin_key
        _write_private_file(layout.admin_key_path, admin_key)
        admin_key_created = True

    if clients or secret_updates:
        control_plane = ControlPlaneService(paths)
        snapshot = control_plane.from_loaded(config=config, secrets=secret_values)
        candidate = control_plane.upsert_graph(
            snapshot,
            clients=clients or None,
            secret_updates=secret_updates or None,
        )
        control_plane.commit(candidate)

    # --- Memory Gateway: private settings.env --------------------------------
    memory_paths = cli_paths(layout.memory_home)
    ensure_initialized(memory_paths, layout.data_dir)
    for name, value in (
        ("MODEL_GATEWAY_BASE_URL", f"{layout.model_url}/v1"),
        ("MODEL_GATEWAY_API_KEY", backend.key),
        ("MODEL_GATEWAY_ALLOW_PRIVATE_HTTP", "true"),
        ("DATABASE_PATH", str(layout.memory_db)),
        ("KNOWLEDGE_DATABASE_PATH", str(layout.knowledge_db)),
        ("AUTH_DATABASE_PATH", str(layout.auth_db)),
        ("EVAL_DIR", str(layout.eval_dir)),
        ("UI_DIST_DIR", str(layout.ui_dist_dir) if layout.ui_dist_dir else None),
        ("GATEWAY_API_KEY", None),
        ("GATEWAY_LEGACY_API_KEY_ENABLED", "false"),
    ):
        update_env_value(memory_paths.settings_env, name, value)
    _chmod(memory_paths.settings_env, 0o600)
    # write_env_atomic keeps a convenience .bak that would hold live secrets.
    memory_paths.settings_env.with_suffix(memory_paths.settings_env.suffix + ".bak").unlink(missing_ok=True)

    # --- First console token --------------------------------------------------
    store = AuthTokenStore(layout.auth_db)
    store.init_db()
    active = [record for record in store.list_tokens() if record.revoked_at is None]
    console_token_created = False
    if not active:
        created = store.create_token(name="first-console", user_id="default", role="console")
        try:
            _write_private_file(layout.console_token_path, created.token)
        except Exception:
            store.revoke_token(created.record.token_id)
            raise
        console_token_created = True

    return BootstrapResult(
        console_token_created=console_token_created,
        admin_key_created=admin_key_created,
        backend_client_created=backend.created,
        console_token_path=str(layout.console_token_path),
        admin_key_path=str(layout.admin_key_path),
    )


class EmbeddedStack:
    """Both uvicorn servers on one asyncio loop, Model Gateway first."""

    def __init__(self, layout: StackLayout) -> None:
        self.layout = layout
        self._servers: list[Any] = []
        self.started = threading.Event()
        self.error: str | None = None

    async def serve(self) -> None:
        import uvicorn

        from model_gateway.config_store import gateway_paths
        from model_gateway.service import create_app as create_model_app

        _ensure_port_free(self.layout.model_port, "Model Gateway")
        _ensure_port_free(self.layout.memory_port, "Memory Gateway")
        model_app = create_model_app(paths=gateway_paths(self.layout.model_home))
        model_server = uvicorn.Server(self._config(uvicorn, model_app, self.layout.model_port))
        self._servers.append(model_server)
        model_task = asyncio.create_task(model_server.serve())
        await self._wait_started(model_server, model_task, "Model Gateway")

        # Imported late: ``app.main`` reads MEMGW_* env vars set by configure().
        # Always build a fresh instance: the MCP session manager inside can only
        # be started once, so re-serving the module-level ``app`` after a stop
        # fails with "can only be called once per instance".
        from app.main import create_app as create_memory_app

        memory_app = create_memory_app()
        memory_server = uvicorn.Server(self._config(uvicorn, memory_app, self.layout.memory_port))
        self._servers.append(memory_server)
        memory_task = asyncio.create_task(memory_server.serve())
        await self._wait_started(memory_server, memory_task, "Memory Gateway")
        self.started.set()
        logger.info("stack running: memory=%s model=%s", self.layout.memory_url, self.layout.model_url)
        await asyncio.gather(model_task, memory_task)

    def stop(self) -> None:
        for server in self._servers:
            server.should_exit = True

    @staticmethod
    def _config(uvicorn: Any, app: Any, port: int) -> Any:
        return uvicorn.Config(
            app,
            host=HOST,
            port=port,
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="on",
            access_log=False,
            log_config=None,
            timeout_graceful_shutdown=5,
        )

    @staticmethod
    async def _wait_started(server: Any, task: asyncio.Task, label: str) -> None:
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while not server.started:
            if task.done():
                exc = task.exception()
                raise RuntimeError(f"{label} 启动失败") from exc
            if time.monotonic() > deadline:
                server.should_exit = True
                raise RuntimeError(f"{label} 启动超时")
            await asyncio.sleep(0.05)


# --- Host-facing API (Kotlin/Java via Chaquopy, or any thread) ----------------

_lock = threading.Lock()
_stack: EmbeddedStack | None = None
_thread: threading.Thread | None = None
_state = "stopped"
_error: str | None = None
_bootstrap: BootstrapResult | None = None
_last_layout: StackLayout | None = None


def start(
    data_dir: str,
    memory_port: int = DEFAULT_MEMORY_PORT,
    model_port: int = DEFAULT_MODEL_PORT,
    ui_dist_dir: str | None = None,
) -> str:
    """Bootstrap and serve on a daemon thread. Returns status() JSON."""

    global _stack, _thread, _state, _error, _bootstrap, _last_layout
    with _lock:
        if _thread is not None and _thread.is_alive():
            # A concurrent caller must not see the intermediate "starting"
            # state and report it as a failure: wait for the real outcome.
            in_progress = _stack
        else:
            in_progress = None
    if in_progress is not None:
        in_progress.started.wait(_START_TIMEOUT_SECONDS + 5)
        _settle(in_progress)
        return status()
    with _lock:
        _state, _error = "starting", None
        try:
            layout = configure(
                data_dir,
                memory_port=int(memory_port),
                model_port=int(model_port),
                ui_dist_dir=ui_dist_dir or None,
            )
            _last_layout = layout
            _attach_file_logging(layout)
            _bootstrap = bootstrap(layout)
        except Exception as exc:  # surfaced to the host UI, not raised across JNI
            _state, _error = "failed", _describe(exc)
            logger.exception("bootstrap failed")
            return status()
        _stack = EmbeddedStack(layout)
        _thread = threading.Thread(target=_run_loop, args=(_stack,), name="memory-platform", daemon=True)
        _thread.start()
    stack = _stack
    if not stack.started.wait(_START_TIMEOUT_SECONDS + 5) and stack.error is None:
        stack.error = "启动超时"
        stack.stop()
    _settle(stack)
    return status()


def _settle(stack: EmbeddedStack) -> None:
    """Translate the runtime's own outcome into the published state."""

    global _state, _error
    with _lock:
        if stack.error is not None:
            _state, _error = "failed", stack.error
        elif stack.started.is_set() and _thread is not None and _thread.is_alive():
            _state, _error = "running", None


def stop() -> str:
    global _state, _stack, _thread
    with _lock:
        stack, thread = _stack, _thread
    if stack is not None:
        stack.stop()
    if thread is not None:
        thread.join(15)
    with _lock:
        if thread is None or not thread.is_alive():
            _state = "stopped"
            # Drop the finished runtime so the next start() builds a fresh one.
            _stack, _thread = None, None
        else:
            _state = "stopping"
    return status()


def status() -> str:
    with _lock:
        stack = _stack
        payload: dict[str, Any] = {
            "state": _state,
            "error": _error,
            "memory_url": stack.layout.memory_url if stack else _last_layout and _last_layout.memory_url,
            "model_url": stack.layout.model_url if stack else _last_layout and _last_layout.model_url,
            "data_dir": str(stack.layout.data_dir) if stack else _last_layout and str(_last_layout.data_dir),
            "bootstrap": asdict(_bootstrap) if _bootstrap else None,
            "runtime": runtime_info(),
        }
    return json.dumps(payload, ensure_ascii=False)


def runtime_info() -> dict[str, Any]:
    fts5 = False
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            fts5 = True
        finally:
            connection.close()
    except sqlite3.OperationalError:
        fts5 = False
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "sqlite": sqlite3.sqlite_version,
        "fts5": fts5,
        "native_extras": {
            name: _importable(name) for name in ("uvloop", "httptools", "websockets", "watchfiles")
        },
    }


_file_logging_attached = False


def _attach_file_logging(layout: StackLayout) -> None:
    """Rotating log under <data_dir>/logs so the host app can export it."""

    global _file_logging_attached
    if _file_logging_attached:
        return
    from logging.handlers import RotatingFileHandler

    log_dir = layout.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _chmod(log_dir, 0o700)
    handler = RotatingFileHandler(log_dir / "stack.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    _file_logging_attached = True


# --- Diagnostics export ----------------------------------------------------------

_NON_SECRET_SUFFIXES = ("_ENABLED", "_PATH", "_ID", "_MODEL", "_URL", "_DIR", "_SPACE_ID", "_HEADER")
_SECRET_NAME_RE = __import__("re").compile(r"(api_?key|secret(?!_ref)|token|password|signing)", __import__("re").IGNORECASE)


def export_diagnostics(data_dir: str | None = None) -> str:
    """Write a diagnostics zip and return its path (JSON string with {"path": ...}).

    Contents: runtime status, rotating service logs (plus logs/*.txt the host
    dropped in, e.g. logcat), settings.env and Model Gateway config.json with
    every secret-looking value redacted, a consistent snapshot of memory.db
    (memories, decision logs, finalize jobs, conversation digests, core memory),
    and readable JSON reports of the last decisions and any unfinished
    finalize jobs. Never includes auth.db, secrets.env or knowledge.db.
    """

    import shutil
    import zipfile
    from datetime import datetime, timezone

    layout = _last_layout
    if layout is None:
        if not data_dir:
            raise RuntimeError("服务尚未启动过，且未提供 data_dir")
        layout = configure(data_dir)
    exports = layout.data_dir / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    _chmod(exports, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = exports / f"memory-platform-diagnostics-{stamp}.zip"
    staging = exports / f".staging-{stamp}"
    staging.mkdir()
    try:
        (staging / "runtime.json").write_text(status(), encoding="utf-8")
        logs = staging / "logs"
        logs.mkdir()
        for item in sorted((layout.data_dir / "logs").glob("*")) if (layout.data_dir / "logs").is_dir() else []:
            if item.is_file():
                shutil.copy2(item, logs / item.name)
        config = staging / "config"
        config.mkdir()
        if layout.settings_env.is_file():
            (config / "settings.env").write_text(_redact_env(layout.settings_env.read_text(encoding="utf-8")), encoding="utf-8")
        model_config = layout.model_home / "config.json"
        if model_config.is_file():
            (config / "model-gateway.config.json").write_text(
                json.dumps(_redact_json(json.loads(model_config.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        db_dir = staging / "db"
        db_dir.mkdir()
        reports = staging / "reports"
        reports.mkdir()
        if layout.memory_db.is_file():
            source = sqlite3.connect(f"file:{layout.memory_db}?mode=ro", uri=True)
            try:
                snapshot = sqlite3.connect(db_dir / "memory.db")
                try:
                    source.backup(snapshot)
                finally:
                    snapshot.close()
                _write_reports(source, reports)
            finally:
                source.close()
        (staging / "README.txt").write_text(_diagnostics_readme(), encoding="utf-8")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
        _chmod(target, 0o600)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for old in sorted(exports.glob("memory-platform-diagnostics-*.zip"))[:-3]:
        old.unlink(missing_ok=True)
    return json.dumps({"path": str(target), "bytes": target.stat().st_size})


def _write_reports(connection: sqlite3.Connection, reports: Path) -> None:
    connection.row_factory = sqlite3.Row

    def rows(sql: str) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in connection.execute(sql)]
        except sqlite3.Error as exc:
            return [{"error": f"{type(exc).__name__}: {exc}"}]

    summary = {
        "memories_by_status": rows("SELECT COALESCE(status, '') AS status, COUNT(*) AS n FROM memories GROUP BY status"),
        "decisions_last_30_days": rows(
            "SELECT decision, COUNT(*) AS n FROM memory_decision_logs "
            "WHERE created_at >= datetime('now', '-30 days') GROUP BY decision"
        ),
        "finalize_jobs_by_status": rows("SELECT status, COUNT(*) AS n FROM chat_finalize_jobs GROUP BY status"),
        "conversations": rows("SELECT COUNT(*) AS n FROM conversation_branch_nodes"),
    }
    (reports / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (reports / "decision_logs.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows("SELECT * FROM memory_decision_logs ORDER BY created_at DESC LIMIT 1000"):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    jobs = rows(
        "SELECT id, user_id, kind, status, attempts, last_error, created_at, updated_at, payload_json "
        "FROM chat_finalize_jobs WHERE status != 'done' "
        "UNION ALL SELECT * FROM (SELECT id, user_id, kind, status, attempts, last_error, created_at, updated_at, payload_json "
        "FROM chat_finalize_jobs WHERE status = 'done' ORDER BY updated_at DESC LIMIT 50)"
    )
    (reports / "finalize_jobs.json").write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    recent = rows(
        "SELECT id, user_id, conversation_id, summary, recent_turns_json, turn_count, updated_at "
        "FROM conversation_branch_nodes ORDER BY updated_at DESC LIMIT 50"
    )
    (reports / "recent_conversations.json").write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")


def _redact_env(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            if _SECRET_NAME_RE.search(name) and not name.upper().endswith(_NON_SECRET_SUFFIXES):
                line = f"{name}=<redacted>"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _redact_json(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _redact_json(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(v, key) for v in value]
    if isinstance(value, str) and _SECRET_NAME_RE.search(key):
        return "<redacted>"
    return value


def _diagnostics_readme() -> str:
    return (
        "Memory Platform diagnostics bundle\n\n"
        "runtime.json                 service state and runtime info\n"
        "logs/stack.log*              service log (uvicorn, memory/model gateway, embedded runtime)\n"
        "logs/logcat.txt              Android process log, when exported from the app\n"
        "config/settings.env          Memory Gateway settings, secret values redacted\n"
        "config/model-gateway.config.json  Model Gateway routes/clients, secret values redacted\n"
        "db/memory.db                 consistent snapshot: memories, memory_decision_logs, chat_finalize_jobs,\n"
        "                             conversation_branch_nodes, core_memory_sections. Contains personal memory content.\n"
        "reports/summary.json         counts by status / decision / job state\n"
        "reports/decision_logs.jsonl  last 1000 extraction decisions with reasons (why a memory was or was not saved)\n"
        "reports/finalize_jobs.json   unfinished chat finalize jobs (memories not yet saved) plus last 50 done\n"
        "reports/recent_conversations.json  last 50 conversation digests with recent turns\n\n"
        "Excluded on purpose: auth.db (token hashes), model-gateway/secrets.env, knowledge.db.\n"
    )


def _run_loop(stack: EmbeddedStack) -> None:
    global _state, _error
    try:
        asyncio.run(stack.serve())
    except BaseException as exc:  # uvicorn raises SystemExit(3) on startup failure
        detail = _describe(exc)
        if isinstance(exc, SystemExit):
            detail = "服务启动失败（uvicorn STARTUP_FAILURE）；详见日志"
        stack.error = detail
        logger.exception("stack terminated")
    finally:
        stack.started.set()
        with _lock:
            if stack.error is not None:
                _state, _error = "failed", stack.error
            else:
                _state = "stopped"


def _ensure_port_free(port: int, label: str) -> None:
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((HOST, port))
    except OSError as exc:
        raise RuntimeError(
            f"{label} 端口 {port} 已被占用（另一个实例或 Termux 里的服务还在运行）"
        ) from exc
    finally:
        probe.close()


def _describe(exc: BaseException) -> str:
    chain = [f"{type(item).__name__}: {item}" for item in _causes(exc)]
    return " <- ".join(chain)


def _causes(exc: BaseException) -> list[BaseException]:
    items: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in items:
        items.append(current)
        current = current.__cause__ or current.__context__
    return items


def _importable(name: str) -> bool:
    try:
        __import__(name)
    except Exception:
        return False
    return True


def _write_private_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, (value + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    _chmod(path, 0o600)


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


# --- CLI for Termux / desktop verification -------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Memory Platform in one loopback process.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--memory-port", type=int, default=DEFAULT_MEMORY_PORT)
    parser.add_argument("--model-port", type=int, default=DEFAULT_MODEL_PORT)
    parser.add_argument("--ui-dist", default=None, help="Web console build directory (index.html + assets/)")
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--bootstrap-only", action="store_true", help="Initialise and exit without serving")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    layout = configure(
        args.data_dir,
        memory_port=args.memory_port,
        model_port=args.model_port,
        ui_dist_dir=args.ui_dist,
    )
    result = bootstrap(layout)
    print(json.dumps({"runtime": runtime_info(), "bootstrap": asdict(result)}, ensure_ascii=False, indent=2))
    print(f"控制台令牌文件：{layout.console_token_path}（首次登录用，cat 查看）")
    if args.bootstrap_only:
        return 0
    stack = EmbeddedStack(layout)
    print(f"Memory Gateway: {layout.memory_url}    Model Gateway: {layout.model_url}")
    try:
        asyncio.run(stack.serve())
    except KeyboardInterrupt:
        stack.stop()
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
