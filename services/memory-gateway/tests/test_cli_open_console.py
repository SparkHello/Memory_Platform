"""`memgw open` 一次性登录链接的单元测试（HTTP 层全部 mock）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from app import cli
from app.cli_config import CliPaths, cli_paths


_CONSOLE_TOKEN = "console-token-for-open-tests"
_LOGIN_CODE = "mgc_one-time-login-code"


def _make_paths(tmp_path: Path, *, port: int = 2026) -> CliPaths:
    paths = cli_paths(tmp_path / "memgw-home")
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.state.write_text(
        json.dumps({"pid": 4321, "port": port}), encoding="utf-8"
    )
    return paths


def _write_console_token(paths: CliPaths, name: str = "gateway.txt") -> None:
    paths.credentials.mkdir(parents=True, exist_ok=True)
    credential = paths.credentials / name
    credential.write_text(_CONSOLE_TOKEN + "\n", encoding="ascii")
    credential.chmod(0o600)


def _capture_browser(monkeypatch, *, succeed: bool = True) -> list[str]:
    opened: list[str] = []

    def _open(url: str) -> bool:
        opened.append(url)
        return succeed

    monkeypatch.setattr(cli.webbrowser, "open", _open)
    return opened


def _stub_post(monkeypatch, post: Any) -> None:
    monkeypatch.setattr(
        cli, "httpx", SimpleNamespace(post=post, HTTPError=httpx.HTTPError)
    )


def _mint_success(calls: list[dict[str, Any]]) -> Any:
    def _post(url: str, **kwargs: Any) -> Any:
        calls.append({"url": url, **kwargs})
        return SimpleNamespace(
            status_code=201,
            json=lambda: {
                "code": _LOGIN_CODE,
                "token_id": "t" * 16,
                "expires_at": "2030-01-01T00:00:00+00:00",
                "expires_in_seconds": 300,
            },
        )

    return _post


def test_open_console_opens_one_time_login_url(tmp_path, monkeypatch, capsys) -> None:
    paths = _make_paths(tmp_path, port=2100)
    _write_console_token(paths)
    calls: list[dict[str, Any]] = []
    _stub_post(monkeypatch, _mint_success(calls))
    opened = _capture_browser(monkeypatch)

    assert cli._open_console(paths=paths) == 0

    assert opened == [f"http://localhost:2100/ui/#login={_LOGIN_CODE}"]
    assert len(calls) == 1
    request = calls[0]
    assert request["url"] == "http://127.0.0.1:2100/auth/console-login-code"
    assert request["headers"]["Authorization"] == f"Bearer {_CONSOLE_TOKEN}"
    # token/code 明文不得出现在终端输出中。
    output = capsys.readouterr()
    assert _CONSOLE_TOKEN not in output.out
    assert _LOGIN_CODE not in output.out


def test_open_console_reads_legacy_gateway_key(tmp_path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    _write_console_token(paths, name="gateway.key")
    calls: list[dict[str, Any]] = []
    _stub_post(monkeypatch, _mint_success(calls))
    opened = _capture_browser(monkeypatch)

    assert cli._open_console(paths=paths) == 0

    assert opened == [f"http://localhost:2026/ui/#login={_LOGIN_CODE}"]
    assert calls[0]["headers"]["Authorization"] == f"Bearer {_CONSOLE_TOKEN}"


def test_open_console_falls_back_when_mint_rejected(
    tmp_path, monkeypatch, capsys
) -> None:
    paths = _make_paths(tmp_path)
    _write_console_token(paths)
    _stub_post(
        monkeypatch,
        lambda url, **kwargs: SimpleNamespace(status_code=404, json=lambda: {}),
    )
    opened = _capture_browser(monkeypatch)

    assert cli._open_console(paths=paths) == 0

    assert opened == ["http://localhost:2026/ui"]
    output = capsys.readouterr().out
    assert "提示" in output
    assert _CONSOLE_TOKEN not in output


def test_open_console_falls_back_when_service_unreachable(
    tmp_path, monkeypatch, capsys
) -> None:
    paths = _make_paths(tmp_path)
    _write_console_token(paths)

    def _post(url: str, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    _stub_post(monkeypatch, _post)
    opened = _capture_browser(monkeypatch)

    assert cli._open_console(paths=paths) == 0

    assert opened == ["http://localhost:2026/ui"]
    output = capsys.readouterr().out
    assert "提示" in output
    assert _CONSOLE_TOKEN not in output


def test_open_console_falls_back_without_credential(
    tmp_path, monkeypatch, capsys
) -> None:
    paths = _make_paths(tmp_path)
    called = False

    def _post(url: str, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("无凭据时不应发起 mint 请求")

    _stub_post(monkeypatch, _post)
    opened = _capture_browser(monkeypatch)

    assert cli._open_console(paths=paths) == 0

    assert called is False
    assert opened == ["http://localhost:2026/ui"]
    assert "提示" in capsys.readouterr().out


def test_open_console_prints_url_when_browser_unavailable(
    tmp_path, monkeypatch, capsys
) -> None:
    paths = _make_paths(tmp_path)
    _write_console_token(paths)
    _stub_post(monkeypatch, _mint_success([]))
    _capture_browser(monkeypatch, succeed=False)

    assert cli._open_console(paths=paths) == 0

    output = capsys.readouterr().out
    assert f"http://localhost:2026/ui/#login={_LOGIN_CODE}" in output
