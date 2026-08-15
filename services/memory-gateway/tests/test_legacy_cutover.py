from __future__ import annotations

import py_compile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CUTOVER = ROOT / "deploy" / "legacy_cutover.py"
INSTALLER_SH = ROOT / "deploy" / "install.sh"
INSTALLER_PS1 = ROOT / "deploy" / "install.ps1"


def _cutover() -> str:
    return CUTOVER.read_text(encoding="utf-8")


def test_legacy_cutover_script_compiles() -> None:
    py_compile.compile(str(CUTOVER), doraise=True)


def test_legacy_cutover_delegates_sqlite_safety_to_audited_helpers() -> None:
    text = _cutover()

    # 编排入口只驱动容器化执行；创建和迁移仍由 audited helpers 完成，
    # 归档验收委托给三条部署路径共用的权威校验器。
    assert "/usr/local/libexec/memory-platform/backup_legacy.py" in text
    assert "/usr/local/libexec/memory-platform/migrate_legacy.py" in text
    assert "/usr/local/libexec/memory-platform/verify_backup.py" in text
    assert "def _copy_sqlite" not in text
    assert "PRAGMA quick_check" not in text
    assert "archive.testzip()" not in text


def test_legacy_cutover_is_offline_read_only_and_fail_closed() -> None:
    text = _cutover()

    assert '"--network", "none", "--read-only"' in text
    assert "target=/legacy,readonly" in text
    assert "target=/backup" in text
    assert "/scratch:rw,noexec,nosuid" in text
    for destination in (
        "/memory-data",
        "/memory-secrets",
        "/model-data",
        "/model-secrets",
    ):
        assert destination in text
    # split 目标卷所有权边界：存在即拒绝，不覆盖不明状态。
    assert "拒绝覆盖不明 split 状态" in text
    assert "旧卷未修改" in text
    assert "GATEWAY_API_KEY" in text  # 拒绝环境变量传密钥
    assert ".stack-installed-v2" in text  # 迁移后复验完成标记


def test_installers_no_longer_embed_legacy_migration() -> None:
    sh_text = INSTALLER_SH.read_text(encoding="utf-8")
    ps1_text = INSTALLER_PS1.read_text(encoding="utf-8-sig")

    for installer in (sh_text, ps1_text):
        assert "migrate_legacy.py" not in installer
        assert "backup_legacy.py" not in installer
        assert "legacy_targets_absent" not in installer
        # 检测到 legacy 布局时 fail-closed 并指向独立迁移工具。
        assert "legacy_cutover.py" in installer
    assert "cleanup_legacy_transaction_volumes" not in sh_text
    assert "legacy_target_volume_exists" not in sh_text
    assert "mount_name" not in sh_text
    assert "Remove-LegacyTransactionVolumes" not in ps1_text
    assert "Test-LegacyTargetVolumeExists" not in ps1_text
    assert "Get-ContainerVolume" not in ps1_text
