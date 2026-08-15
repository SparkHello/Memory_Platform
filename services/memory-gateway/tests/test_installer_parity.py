"""安装器双实现(deploy/install.sh 与 deploy/install.ps1)与配套文件的常量一致性检查。

双实现是已知漂移风险(历史上默认版本号曾在 tag 发布后落后多个版本)。
本测试只读文本,不执行安装器,全平台可跑。
"""

from __future__ import annotations

import re
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[3]


def _default_release_from_sh() -> str:
    text = (PLATFORM_ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    match = re.search(r'MEMORY_PLATFORM_VERSION:-(v[^}]+)\}', text)
    assert match, "install.sh 缺少 MEMORY_PLATFORM_VERSION 默认版本"
    return match.group(1)


def _default_release_from_ps1() -> str:
    raw = (PLATFORM_ROOT / "deploy" / "install.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "install.ps1 必须带 UTF-8 BOM(PowerShell 5.1 需要)"
    text = raw.decode("utf-8-sig")
    match = re.search(r'\$release = "(v[^"]+)"', text)
    assert match, "install.ps1 缺少 $release 默认版本"
    return match.group(1)


def _default_release_from_cutover() -> str:
    text = (PLATFORM_ROOT / "deploy" / "legacy_cutover.py").read_text(encoding="utf-8")
    match = re.search(r'MEMORY_PLATFORM_VERSION", "(v[^"]+)"', text)
    assert match, "legacy_cutover.py 缺少 MEMORY_PLATFORM_VERSION 默认版本"
    return match.group(1)


def _default_releases_from_user_compose() -> set[str]:
    text = (PLATFORM_ROOT / "deploy" / "docker-compose.user.yml").read_text(
        encoding="utf-8"
    )
    releases = set(re.findall(r'memory-platform-(?:init|model|memory):(v[^"}]+)\}', text))
    assert releases, "docker-compose.user.yml 缺少默认镜像 tag"
    return releases


def test_installer_default_release_is_in_sync_across_implementations():
    sh_release = _default_release_from_sh()
    assert _default_release_from_ps1() == sh_release
    assert _default_release_from_cutover() == sh_release
    assert _default_releases_from_user_compose() == {sh_release}


def test_installers_share_candidate_validator_and_readiness_vocabulary() -> None:
    shell = (PLATFORM_ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    powershell = (PLATFORM_ROOT / "deploy" / "install.ps1").read_text(
        encoding="utf-8-sig"
    )

    validator_path = "/usr/local/libexec/memory-platform/validate_compose.py"
    assert validator_path in shell
    assert validator_path in powershell
    assert shell.count("validate_candidate_topology ") == 2
    assert powershell.count("Test-RenderedCandidateTopology `") == 2
    for state in ("ready", "not_ready", "absent", "unknown"):
        assert state in shell
        assert state in powershell
    for isolation_token in (
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "no-new-privileges:true",
        "65534:65534",
    ):
        assert isolation_token in shell
        assert isolation_token in powershell


def test_installers_share_typed_planner_actions_and_acceptance_fields() -> None:
    shell = (PLATFORM_ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    powershell = (PLATFORM_ROOT / "deploy" / "install.ps1").read_text(
        encoding="utf-8-sig"
    )

    planner_path = "/usr/local/libexec/memory-platform/plan_install.py"
    assert planner_path in shell
    assert planner_path in powershell
    for action in ("noop", "repair", "upgrade"):
        assert action in shell
        assert action in powershell
    for powershell_field, shell_field in (
        ("RepairScope", "PLAN_REPAIR_SCOPE"),
        ("AcceptMemoryReadiness", "PLAN_ACCEPT_MEMORY_READINESS"),
        ("AcceptModelReadiness", "PLAN_ACCEPT_MODEL_READINESS"),
        ("AcceptHostReadiness", "PLAN_ACCEPT_HOST_READINESS"),
    ):
        assert powershell_field in powershell
        assert shell_field in shell
    assert "--no-deps --force-recreate model-gateway" in shell
    assert "--no-deps --force-recreate model-gateway" in powershell
    assert "--no-deps --force-recreate memory-gateway" in shell
    assert "--no-deps --force-recreate memory-gateway" in powershell


def test_all_cutover_paths_share_the_authoritative_backup_validator() -> None:
    shell = (PLATFORM_ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    powershell = (PLATFORM_ROOT / "deploy" / "install.ps1").read_text(
        encoding="utf-8-sig"
    )
    legacy = (PLATFORM_ROOT / "deploy" / "legacy_cutover.py").read_text(
        encoding="utf-8"
    )
    verifier_path = "/usr/local/libexec/memory-platform/verify_backup.py"
    verifier = (PLATFORM_ROOT / "deploy" / "verify_backup.py").read_text(
        encoding="utf-8"
    )
    dockerfile = (PLATFORM_ROOT / "Dockerfile").read_text(encoding="utf-8")

    for implementation in (shell, powershell, legacy):
        assert implementation.count(verifier_path) == 1
        assert "type=volume,target=/tmp,volume-nocopy" in implementation
        assert "archive.testzip()" not in implementation
        assert "PRAGMA quick_check" not in implementation
    assert "quiesced_verify_image=$INIT_IMAGE" in shell
    assert "$verifyImage = $script:InitImage" in powershell
    assert "from app.stack_backup import validate_stack_backup" in verifier
    assert "COPY deploy/verify_backup.py" in dockerfile
