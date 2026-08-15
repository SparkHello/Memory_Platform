#!/usr/bin/env python3
"""One-shot legacy single-volume -> split-volume cutover (host-side driver).

Older releases ran the legacy -> split migration inside the release
installers.  That orchestration now lives here so the installers only handle
fresh and split layouts.  This script is deliberately self-contained
(stdlib only) so it can be fetched next to ``install.sh`` and run directly::

    curl -fsSL "$REPO_RAW/deploy/legacy_cutover.py" -o legacy-cutover.py
    python3 legacy-cutover.py

Environment inputs mirror ``deploy/install.sh``:

- ``MEMORY_PLATFORM_VERSION``   release tag to migrate to (default v0.5.1)
- ``MEMORY_PLATFORM_DIR``       install directory (auto-discovered if unique)
- ``COMPOSE_PROJECT_NAME``      explicit compose project override
- ``MEMORY_HOST`` / ``MEMORY_PORT``  publish address (default 127.0.0.1:2026)
- ``MEMORY_IMAGE_REGISTRY``     registry mirror host (default ghcr.io)

The heavy lifting (SQLite snapshot validation, portable backup assembly,
allow-listed migration) still runs inside the signed init image via the
audited ``backup_legacy.py`` / ``migrate_legacy.py`` helpers; this script only
orchestrates the Docker calls.  The legacy volume is mounted read-only and is
never modified, so it remains the rollback anchor until the operator deletes
it explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

COMPOSE_NAME = "docker-compose.user.yml"
SPLIT_VOLUME_KEYS = ("memory-data", "memory-secrets", "model-data", "model-secrets")
LEGACY_VOLUME_KEY = "memory-platform-data"
FORBIDDEN_ENV_KEYS = {
    "GATEWAY_API_KEY",
    "MEMORY_CONSOLE_ADMIN_KEY",
    "COMPOSE_ENV_FILES",
    "COMPOSE_DISABLE_ENV_FILE",
    "COMPOSE_PROFILES",
    "COMPOSE_FILE",
    "COMPOSE_PATH_SEPARATOR",
}

RELEASE = os.environ.get("MEMORY_PLATFORM_VERSION", "v0.5.1")
IMAGE_REGISTRY = os.environ.get("MEMORY_IMAGE_REGISTRY", "ghcr.io")


class CutoverError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise CutoverError(message)


def say(message: str) -> None:
    print(message, flush=True)


def run(arguments: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        arguments,
        text=True,
        capture_output=True,
        check=False,
        **kwargs,
    )


def docker(*arguments: str) -> subprocess.CompletedProcess:
    return run(["docker", *arguments])


def validate_environment() -> None:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", RELEASE):
        fail("MEMORY_PLATFORM_VERSION 必须是 vX.Y.Z 形式的发布版本。")
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", IMAGE_REGISTRY) or "/" in IMAGE_REGISTRY:
        fail("MEMORY_IMAGE_REGISTRY 只能是 registry 主机名（可带端口），如 ghcr.nju.edu.cn。")
    if os.environ.get("GATEWAY_API_KEY") or os.environ.get("MEMORY_CONSOLE_ADMIN_KEY"):
        fail("迁移工具不接受环境变量中的密钥；凭据只写入 credentials/*.txt。")
    if shutil.which("docker") is None:
        fail("未找到 Docker。")
    if docker("info").returncode != 0:
        fail("Docker 尚未运行。")
    if docker("compose", "version").returncode != 0:
        fail("需要 Docker Compose v2。")


def existing_install_dirs() -> list[str]:
    result = docker(
        "ps", "-a",
        "--filter", "label=com.docker.compose.service=memory-platform",
        "--format", '{{.Label "com.docker.compose.project.working_dir"}}',
    )
    dirs: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and line not in dirs:
            dirs.append(line)
    return dirs


def resolve_install_dir() -> Path:
    requested = os.environ.get("MEMORY_PLATFORM_DIR", "").strip()
    if requested:
        install_dir = Path(requested)
    else:
        discovered = existing_install_dirs()
        if len(discovered) > 1:
            fail("检测到多套安装；请显式设置 MEMORY_PLATFORM_DIR。")
        if discovered:
            install_dir = Path(discovered[0])
        else:
            home = os.environ.get("HOME", "").strip()
            if not home:
                fail("无法确定用户目录；请显式设置 MEMORY_PLATFORM_DIR。")
            install_dir = Path(home) / "memory-platform"
    if not install_dir.is_dir():
        fail(f"安装目录不存在：{install_dir}")
    return install_dir.resolve()


def compose_env_value(env_path: Path, key: str) -> str:
    value = ""
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(key + "="):
                value = line[len(key) + 1:].rstrip("\r")
    return value


def compose_services(compose_path: Path, project: str) -> list[str]:
    result = docker("compose", "-p", project, "-f", str(compose_path), "config", "--services")
    if result.returncode != 0:
        fail("无法解析现有 Compose；拒绝猜测迁移。")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def resolve_project(install_dir: Path, env_path: Path) -> str:
    requested = os.environ.get("COMPOSE_PROJECT_NAME", "").strip()
    stored = compose_env_value(env_path, "COMPOSE_PROJECT_NAME")
    result = docker(
        "ps", "-a",
        "--filter", f"label=com.docker.compose.project.working_dir={install_dir}",
        "--format", '{{.Label "com.docker.compose.project"}}',
    )
    discovered = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    if len(discovered) > 1:
        fail("安装目录对应多个旧 Compose project；拒绝猜测数据归属。")
    project = discovered[0] if discovered else ""
    if project:
        if requested and requested != project:
            fail("COMPOSE_PROJECT_NAME 与旧容器 project 身份冲突；旧栈未修改。")
        if stored and stored != project:
            fail(".env 的 COMPOSE_PROJECT_NAME 与旧容器身份冲突；拒绝迁移。")
    else:
        if requested and stored and requested != stored:
            fail("本次 COMPOSE_PROJECT_NAME 与现有 .env 冲突；拒绝切换数据 project。")
        project = requested or stored
    if not project:
        project = re.sub(r"[^a-z0-9_-]", "", install_dir.name.lower())
    if not project:
        project = "memory-platform"
    if not re.fullmatch(r"[a-z0-9_-]+", project):
        fail("Compose project 名无效。")
    return project


def labeled_volume(project: str, key: str) -> str:
    result = docker(
        "volume", "ls",
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", f"label=com.docker.compose.volume={key}",
        "--format", "{{.Name}}",
    )
    for line in result.stdout.splitlines():
        if line.strip():
            return line.strip()
    return ""


def split_target_volume_exists(project: str, key: str) -> bool:
    if labeled_volume(project, key):
        return True
    expected = f"{project}_{key}"
    result = docker("volume", "inspect", expected, "--format", "{{.Name}}")
    return result.returncode == 0 and expected in result.stdout.split()


def container_mount(container: str, destination: str) -> str:
    result = docker(
        "inspect", container,
        "--format",
        "{{range .Mounts}}{{if eq .Destination \"" + destination + "\"}}{{.Name}}{{end}}{{end}}",
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def compose_ps_id(compose_path: Path, project: str, service: str, *, all_containers: bool = False) -> str:
    arguments = ["compose", "-p", project, "-f", str(compose_path), "ps"]
    arguments.append("-aq" if all_containers else "-q")
    arguments.append(service)
    result = docker(*arguments)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def digest_ref(tag: str) -> str:
    repository = tag.rsplit(":", 1)[0]
    result = docker(
        "image", "inspect", tag,
        "--format", "{{range .RepoDigests}}{{println .}}{{end}}",
    )
    prefix = repository + "@sha256:"
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith(prefix) and re.fullmatch(
            r"[0-9a-f]{64}", candidate[len(prefix):]
        ):
            return candidate
    fail("无法把发布镜像解析为不可变 digest。")
    raise AssertionError("unreachable")


VERIFY_ARCHIVE_SCRIPT = """
import os, shutil, sqlite3, sys, tempfile, zipfile
archive = zipfile.ZipFile("/backup/verify.zip")
corrupt = archive.testzip()
assert corrupt is None, f"CRC mismatch: {corrupt}"
for member in archive.namelist():
    if not member.endswith(".db"):
        continue
    with tempfile.NamedTemporaryFile(dir="/tmp", suffix=".db", delete=False) as staged:
        with archive.open(member) as source:
            shutil.copyfileobj(source, staged)
        staged_path = staged.name
    connection = sqlite3.connect(staged_path)
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
        os.unlink(staged_path)
    assert row and row[0] == "ok", f"quick_check failed: {member}"
"""


def wait_http(url: str, attempts: int) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def write_split_environment(
    env_path: Path,
    *,
    init_image: str,
    model_image: str,
    memory_image: str,
    host: str,
    port: str,
    project: str,
    host_uid: str,
    host_gid: str,
) -> None:
    managed = {
        "MEMORY_PLATFORM_INIT_IMAGE": init_image,
        "MEMORY_PLATFORM_MODEL_IMAGE": model_image,
        "MEMORY_PLATFORM_MEMORY_IMAGE": memory_image,
        "MEMORY_CREDENTIAL_DIR": "./credentials",
        "HOST_UID": host_uid,
        "HOST_GID": host_gid,
        "MEMORY_HOST": host,
        "MEMORY_PORT": port,
        "COMPOSE_PROJECT_NAME": project,
    }
    kept: list[str] = []
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            key = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line else ""
            if not key or key in FORBIDDEN_ENV_KEYS or key in managed:
                continue
            kept.append(line)
    content = "\n".join(kept + [f"{key}={value}" for key, value in managed.items()]) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.cutover.", dir=env_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, env_path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def main() -> int:
    say(f"==> Memory Platform 旧单卷一次性迁移（目标版本 {RELEASE}）")
    validate_environment()
    install_dir = resolve_install_dir()
    compose_path = install_dir / COMPOSE_NAME
    env_path = install_dir / ".env"
    credentials_dir = install_dir / "credentials"
    backups_dir = install_dir / "backups"
    if not compose_path.is_file():
        fail(f"安装目录缺少 {COMPOSE_NAME}；拒绝猜测迁移。")
    project = resolve_project(install_dir, env_path)
    os.chdir(install_dir)

    services = compose_services(compose_path, project)
    if "memory-gateway" in services:
        say("当前安装已是四卷（split）布局，无需迁移；请直接使用 deploy/install.sh 升级。")
        return 0
    if "memory-platform" not in services:
        fail("现有 Compose 不是可识别的 Memory Platform 旧单卷栈；拒绝覆盖。")

    say("==> 校验迁移边界：split 目标卷必须全部不存在")
    for key in SPLIT_VOLUME_KEYS:
        if split_target_volume_exists(project, key):
            fail(f"legacy 迁移目标卷 {key} 已存在；拒绝覆盖不明 split 状态。")

    old_container = compose_ps_id(compose_path, project, "memory-platform", all_containers=True)
    if not old_container:
        if not labeled_volume(project, LEGACY_VOLUME_KEY):
            fail("现有 Compose 没有同 project 的容器或数据卷；拒绝在空 project 上迁移。")
        say("==> 按旧 Compose 启动服务以定位旧数据卷")
        if docker("compose", "-p", project, "-f", str(compose_path), "up", "-d", "--pull", "never").returncode != 0:
            fail("无法按旧 Compose 启动服务以定位旧数据卷；现有数据未修改。")
        old_container = compose_ps_id(compose_path, project, "memory-platform", all_containers=True)
    if not old_container:
        fail("找不到旧 Memory 容器；拒绝在未备份状态下迁移。")
    legacy_volume = container_mount(old_container, "/data")
    if not legacy_volume:
        fail("无法定位旧单卷；未修改任何状态。")

    credentials_dir.mkdir(mode=0o700, exist_ok=True)
    backups_dir.mkdir(mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
    old_compose_backup = backups_dir / f"pre-upgrade-{stamp}.compose.yml"
    shutil.copyfile(compose_path, old_compose_backup)
    os.chmod(old_compose_backup, 0o600)
    old_env_bytes = env_path.read_bytes() if env_path.is_file() else None

    host = os.environ.get("MEMORY_HOST", "").strip() or compose_env_value(env_path, "MEMORY_HOST") or "127.0.0.1"
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", host):
        fail("MEMORY_HOST 必须是本机可绑定的 IPv4 地址。")
    port = os.environ.get("MEMORY_PORT", "").strip() or compose_env_value(env_path, "MEMORY_PORT") or "2026"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        fail("MEMORY_PORT 必须是 1–65535 的整数。")
    host_uid = str(os.getuid()) if hasattr(os, "getuid") else ""
    host_gid = str(os.getgid()) if hasattr(os, "getgid") else ""

    repo_raw = f"https://raw.githubusercontent.com/SparkHello/Memory_Platform/{RELEASE}"
    init_tag = f"{IMAGE_REGISTRY}/sparkhello/memory-platform-init:{RELEASE}"
    model_tag = f"{IMAGE_REGISTRY}/sparkhello/memory-platform-model:{RELEASE}"
    memory_tag = f"{IMAGE_REGISTRY}/sparkhello/memory-platform-memory:{RELEASE}"

    say(f"==> 下载 {RELEASE} Compose 并拉取三枚 semver 发布镜像")
    candidate_fd, candidate_name = tempfile.mkstemp(
        prefix=f".{COMPOSE_NAME}.cutover.", dir=install_dir
    )
    os.close(candidate_fd)
    candidate_compose: Path | None = Path(candidate_name)

    def rollback_old_stack() -> None:
        say("==> 迁移未完成，恢复旧 Compose/.env 与旧服务")
        shutil.copyfile(old_compose_backup, compose_path)
        os.chmod(compose_path, 0o600)
        if old_env_bytes is not None:
            env_path.write_bytes(old_env_bytes)
            os.chmod(env_path, 0o600)
        else:
            env_path.unlink(missing_ok=True)
        if docker(
            "compose", "-p", project, "-f", str(compose_path), "up", "-d", "--pull", "never"
        ).returncode != 0:
            say("warning: 旧服务自动恢复失败；旧单卷与 backups/ 未修改，可手工 compose up -d 恢复。")

    try:
        try:
            with urllib.request.urlopen(
                f"{repo_raw}/deploy/{COMPOSE_NAME}", timeout=60
            ) as response:
                candidate_compose.write_bytes(response.read())
        except Exception:
            fail("下载发布版 Compose 失败；旧服务未变。raw.githubusercontent.com 在部分网络不可达：请先设置代理再重跑。")
        os.chmod(candidate_compose, 0o600)
        candidate_env = {
            "MEMORY_PLATFORM_INIT_IMAGE": init_tag,
            "MEMORY_PLATFORM_MODEL_IMAGE": model_tag,
            "MEMORY_PLATFORM_MEMORY_IMAGE": memory_tag,
            "MEMORY_CREDENTIAL_DIR": "./credentials",
            "HOST_UID": host_uid,
            "HOST_GID": host_gid,
            "MEMORY_HOST": host,
            "MEMORY_PORT": port,
        }
        config_env = {**os.environ, **candidate_env}
        if run(
            ["docker", "compose", "-p", project, "-f", str(candidate_compose), "config"],
            env=config_env,
        ).returncode != 0:
            fail("候选 Compose 语法无效；旧服务未变。")
        if run(
            ["docker", "compose", "-p", project, "-f", str(candidate_compose), "pull"],
            env=config_env,
        ).returncode != 0:
            fail("镜像拉取失败；旧服务未变。GHCR 在部分网络不可达：可设 MEMORY_IMAGE_REGISTRY=<GHCR 镜像站域名> 重跑。")
        init_image = digest_ref(init_tag)
        model_image = digest_ref(model_tag)
        memory_image = digest_ref(memory_tag)
        if run(
            ["docker", "compose", "-p", project, "-f", str(candidate_compose), "config"],
            env={
                **os.environ,
                **candidate_env,
                "MEMORY_PLATFORM_INIT_IMAGE": init_image,
                "MEMORY_PLATFORM_MODEL_IMAGE": model_image,
                "MEMORY_PLATFORM_MEMORY_IMAGE": memory_image,
            },
        ).returncode != 0:
            fail("digest 固定后的 Compose 无效；旧服务未变。")

        say("==> 停止旧服务写入")
        if docker("compose", "-p", project, "-f", str(compose_path), "stop").returncode != 0:
            rollback_old_stack()
            fail("无法停止旧服务；未开始迁移。")

        say("==> 从只读旧单卷创建并复验 v2 便携备份")
        backup_name = f"pre-upgrade-{stamp}-quiesced.zip"
        backup_path = backups_dir / backup_name
        backup_result = docker(
            "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
            "--mount", f"type=volume,source={legacy_volume},target=/legacy,readonly",
            "--mount", f"type=bind,source={backups_dir},target=/backup",
            "--tmpfs", "/scratch:rw,noexec,nosuid,size=33554432",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=134217728",
            "--entrypoint", "python", init_image,
            "/usr/local/libexec/memory-platform/backup_legacy.py",
            backup_name, host_uid or "0", host_gid or "0",
        )
        if backup_result.returncode != 0 or not backup_path.is_file() or backup_path.stat().st_size == 0:
            backup_path.unlink(missing_ok=True)
            rollback_old_stack()
            fail("无法从只读旧单卷创建完整 v2 备份；旧卷未修改。")
        os.chmod(backup_path, 0o600)
        # 复验：归档每个成员通过 ZIP CRC，且每个 SQLite 库重新打开后 quick_check=ok。
        verify_result = docker(
            "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--mount", f"type=bind,source={backup_path},target=/backup/verify.zip,readonly",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=268435456",
            "--entrypoint", "python", init_image, "-c", VERIFY_ARCHIVE_SCRIPT,
        )
        if verify_result.returncode != 0:
            rollback_old_stack()
            fail("升级前备份复验失败；旧卷未修改，请保留 backups/ 排查。")

        say("==> 创建四个 split 目标卷并执行离线迁移")
        create_env = {
            **os.environ,
            **candidate_env,
            "MEMORY_PLATFORM_INIT_IMAGE": init_image,
            "MEMORY_PLATFORM_MODEL_IMAGE": model_image,
            "MEMORY_PLATFORM_MEMORY_IMAGE": memory_image,
            "COMPOSE_PROJECT_NAME": project,
        }
        if run(
            ["docker", "compose", "-p", project, "-f", str(candidate_compose), "create", "stack-init"],
            env=create_env,
        ).returncode != 0:
            rollback_old_stack()
            fail("无法创建新分卷；旧服务已恢复。")
        init_container = compose_ps_id(candidate_compose, project, "stack-init", all_containers=True)
        split_volumes = {
            key: container_mount(init_container, f"/{key}") for key in SPLIT_VOLUME_KEYS
        } if init_container else {}
        run(
            ["docker", "compose", "-p", project, "-f", str(candidate_compose), "rm", "-f", "stack-init"],
            env=create_env,
        )
        if any(not volume for volume in split_volumes.values()):
            rollback_old_stack()
            fail("新分卷解析失败；旧服务已恢复。")
        migrate_result = docker(
            "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
            "-e", f"HOST_UID={host_uid}", "-e", f"HOST_GID={host_gid}",
            "--mount", f"type=volume,source={legacy_volume},target=/legacy,readonly",
            "--mount", f"type=volume,source={split_volumes['memory-data']},target=/memory-data",
            "--mount", f"type=volume,source={split_volumes['memory-secrets']},target=/memory-secrets",
            "--mount", f"type=volume,source={split_volumes['model-data']},target=/model-data",
            "--mount", f"type=volume,source={split_volumes['model-secrets']},target=/model-secrets",
            "--mount", f"type=bind,source={credentials_dir},target=/credentials",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=134217728",
            "--entrypoint", "python", init_image,
            "/usr/local/libexec/memory-platform/migrate_legacy.py",
        )
        if migrate_result.returncode != 0:
            rollback_old_stack()
            fail("旧单卷离线迁移失败；旧卷未修改，split 半成品卷保留供排查。")

        say("==> 复验迁移结果（完成标记与凭据交付）")
        marker_check = docker(
            "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--mount", f"type=volume,source={split_volumes['memory-data']},target=/memory-data,readonly",
            "--mount", f"type=volume,source={split_volumes['model-data']},target=/model-data,readonly",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=33554432",
            "--entrypoint", "python", init_image, "-c",
            "import pathlib,sys; sys.exit(0 if pathlib.Path('/memory-data/.stack-installed-v2').is_file() and pathlib.Path('/model-data/.stack-installed-v2').is_file() else 1)",
        )
        credentials_ok = all(
            any(
                (credentials_dir / f"{role}.{extension}").is_file()
                and (credentials_dir / f"{role}.{extension}").stat().st_size > 0
                for extension in ("txt", "key")
            )
            for role in ("gateway", "admin")
        )
        if marker_check.returncode != 0 or not credentials_ok:
            rollback_old_stack()
            fail("迁移复验失败；旧卷未修改，请保留 backups/ 与 split 卷排查。")

        say("==> 换入四卷发布 Compose 并启动新栈")
        write_split_environment(
            env_path,
            init_image=init_image,
            model_image=model_image,
            memory_image=memory_image,
            host=host,
            port=port,
            project=project,
            host_uid=host_uid,
            host_gid=host_gid,
        )
        os.replace(candidate_compose, compose_path)
        os.chmod(compose_path, 0o600)
        candidate_compose = None
        live_env = {
            **os.environ,
            "MEMORY_PLATFORM_INIT_IMAGE": init_image,
            "MEMORY_PLATFORM_MODEL_IMAGE": model_image,
            "MEMORY_PLATFORM_MEMORY_IMAGE": memory_image,
            "MEMORY_CREDENTIAL_DIR": "./credentials",
            "HOST_UID": host_uid,
            "HOST_GID": host_gid,
            "MEMORY_HOST": host,
            "MEMORY_PORT": port,
            "COMPOSE_PROJECT_NAME": project,
        }
        if run(
            ["docker", "compose", "-p", project, "-f", str(compose_path), "up", "-d"],
            env=live_env,
        ).returncode != 0:
            rollback_old_stack()
            fail("新栈启动失败；旧服务已恢复，split 卷与 backups/ 保留供排查。")
        if not wait_http(f"http://127.0.0.1:{port}/health", 180) or not wait_http(
            f"http://127.0.0.1:{port}/readyz", 90
        ):
            rollback_old_stack()
            fail("新栈健康检查未通过；旧服务已恢复，split 卷与 backups/ 保留供排查。")
        model_container = compose_ps_id(compose_path, project, "model-gateway")
        model_ports = docker("port", model_container).stdout.strip() if model_container else ""
        memory_port = docker(
            "compose", "-p", project, "-f", str(compose_path), "port", "memory-gateway", "2026"
        ).stdout.strip()
        if model_ports or not memory_port.endswith(f":{port}"):
            rollback_old_stack()
            fail("新栈端口契约不成立；旧服务已恢复，请保留 volumes 与 backups/ 排查。")
    finally:
        if candidate_compose is not None and candidate_compose.is_file():
            candidate_compose.unlink(missing_ok=True)

    say("")
    say(f"旧单卷已迁移到 Memory Platform {RELEASE} 四卷布局")
    say(f"  Web Console:  http://127.0.0.1:{port}/ui/")
    say(f"  Client URL:   http://127.0.0.1:{port}/v1")
    say(f"  升级前备份:   {backup_path}")
    say(f"  旧 Compose 快照: {old_compose_backup}")
    say("Console 凭据在迁移兼容期可能仍是 legacy all-scope；请尽快创建按设备 Chat/MCP token。")
    say(f"旧单卷 {legacy_volume} 仍保留用于观察期回滚；完成客户端/备份验证后再显式删除。")
    say("后续版本升级请使用 deploy/install.sh（Windows 为 install.ps1）。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CutoverError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
