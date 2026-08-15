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
