"""Daemon process management for the Model Gateway CLI.

Kept separate from the CLI argument/business layer so that the
start/stop/status handlers and the web console share one implementation of
state-file reading, PID liveness, process identity checks, and self probing.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Mapping

import httpx

from model_gateway.config_store import GatewayPaths


def _gateway_responding(url: str) -> bool:
    try:
        # This is always a self-probe.  In particular, do not route loopback
        # traffic through a Windows system proxy that lacks a localhost bypass.
        response = httpx.get(
            f"{url.rstrip('/')}/health",
            timeout=0.5,
            trust_env=False,
        )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") in {"ok", "warning"}


def _read_state(paths: GatewayPaths) -> dict[str, Any] | None:
    if not paths.state.exists():
        return None
    try:
        payload = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    return payload


def _write_state(paths: GatewayPaths, payload: Mapping[str, Any]) -> None:
    paths.state.parent.mkdir(parents=True, exist_ok=True)
    temporary = paths.state.with_name(f".{paths.state.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, paths.state)
    finally:
        temporary.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # On Windows signal 0 is CTRL_C_EVENT, not a POSIX-style existence
        # probe.  os.kill(pid, 0) can therefore interrupt the very process we
        # are checking (and, without a separate group, the caller as well).
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        process_query_limited_information = 0x1000
        still_active = 259
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.name != "nt":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if result.returncode != 0 or result.stdout.strip().startswith("Z"):
            return False
    return True


def _state_process_matches(state: Mapping[str, Any], paths: GatewayPaths) -> bool:
    pid = state.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not _pid_alive(pid):
        return False
    expected_home = str(paths.home.resolve())
    if str(state.get("home") or "") != expected_home:
        return False
    command = _process_command(pid)
    if command is None:
        return False
    return (
        "model_gateway.cli" in command
        and "serve" in command
        and expected_home in command
    )


def _process_command(pid: int) -> str | None:
    if os.name == "nt":
        script = (
            "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = "
            f"{pid}\"; if ($null -ne $p) {{ $p.CommandLine }}"
        )
        for executable in ("powershell.exe", "pwsh.exe"):
            try:
                result = subprocess.run(
                    [
                        executable,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        script,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            command = result.stdout.strip()
            if result.returncode == 0 and command:
                return command
        return None
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    command = result.stdout.strip()
    return command if result.returncode == 0 and command else None
