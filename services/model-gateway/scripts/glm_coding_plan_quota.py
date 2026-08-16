#!/usr/bin/env python3
"""Query the remaining GLM Coding Plan quota without making a model request."""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone, tzinfo
from typing import Any, Protocol
from urllib.parse import urlsplit


ENDPOINTS = {
    "bigmodel": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
    "zai": "https://api.z.ai/api/monitor/usage/quota/limit",
}
MAX_RESPONSE_BYTES = 1024 * 1024


class QuotaQueryError(RuntimeError):
    """Raised when the remote quota cannot be queried or interpreted safely."""


class _Response(Protocol):
    status: int

    def read(self, amount: int | None = None) -> bytes: ...


class _Connection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> _Response: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[..., _Connection]


def query_quota(
    api_key: str,
    *,
    platform: str = "bigmodel",
    timeout: float = 15.0,
    connection_factory: ConnectionFactory = http.client.HTTPSConnection,
) -> Mapping[str, Any]:
    """Fetch one quota snapshot from the selected official GLM endpoint."""

    key = api_key.strip()
    if not key:
        raise QuotaQueryError("API Key 不能为空")
    if "\r" in key or "\n" in key:
        raise QuotaQueryError("API Key 格式无效")

    try:
        endpoint = ENDPOINTS[platform]
    except KeyError as exc:
        raise QuotaQueryError(f"不支持的平台：{platform}") from exc

    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise QuotaQueryError("额度查询地址必须是有效的 HTTPS 地址")

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    connection = connection_factory(
        parsed.hostname,
        parsed.port or 443,
        timeout=timeout,
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Authorization": key,
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise QuotaQueryError(f"请求额度失败：{exc}") from exc
    finally:
        connection.close()

    if len(body) > MAX_RESPONSE_BYTES:
        raise QuotaQueryError("额度接口响应过大，已拒绝处理")

    if response.status != 200:
        try:
            payload = _decode_payload(body)
        except QuotaQueryError:
            payload = {}
        detail = _error_detail(payload)
        if response.status in {401, 403}:
            raise QuotaQueryError(f"API Key 无效或无权查询额度{detail}")
        raise QuotaQueryError(f"额度接口返回 HTTP {response.status}{detail}")

    payload = _decode_payload(body)
    if payload.get("success") is False:
        raise QuotaQueryError(f"额度查询失败{_error_detail(payload)}")
    code = payload.get("code")
    if code not in (None, 0, 200, "0", "200"):
        raise QuotaQueryError(f"额度查询失败{_error_detail(payload)}")
    return payload


def format_quota(
    payload: Mapping[str, Any],
    *,
    local_timezone: tzinfo | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Convert the quota payload to concise terminal-only output lines."""

    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        raise QuotaQueryError("额度接口响应缺少 data 对象")
    limits = data.get("limits")
    if not isinstance(limits, list):
        raise QuotaQueryError("额度接口响应缺少 limits 列表")
    reference_now = now or datetime.now(tz=timezone.utc)
    if reference_now.tzinfo is None:
        raise QuotaQueryError("当前时间必须包含时区")

    token_limits = [
        item
        for item in limits
        if isinstance(item, Mapping) and item.get("type") == "TOKENS_LIMIT"
    ]
    token_limits.sort(key=_token_sort_key)
    time_limits = [
        item
        for item in limits
        if isinstance(item, Mapping) and item.get("type") == "TIME_LIMIT"
    ]

    lines: list[str] = []
    for index, item in enumerate(token_limits):
        label = _token_label(item, index=index, total=len(token_limits))
        lines.append(
            _percentage_line(
                label,
                item,
                local_timezone=local_timezone,
                countdown_now=reference_now if label == "5 小时" else None,
            )
        )

    for index, item in enumerate(time_limits):
        label = "MCP 月度" if len(time_limits) == 1 else f"MCP 月度 {index + 1}"
        lines.append(_time_limit_line(label, item, local_timezone=local_timezone))

    if not lines:
        raise QuotaQueryError("当前账号没有可显示的 Coding Plan 额度")
    return lines


def _decode_payload(body: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuotaQueryError("额度接口返回了无法解析的数据") from exc
    if not isinstance(payload, Mapping):
        raise QuotaQueryError("额度接口返回格式无效")
    return payload


def _error_detail(payload: Mapping[str, Any]) -> str:
    for key in ("msg", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return f"：{value.strip()}"
    return ""


def _token_sort_key(item: Mapping[str, Any]) -> tuple[int, float]:
    unit = _number(item.get("unit"))
    number = _number(item.get("number"))
    if unit == 3 and number == 5:
        priority = 0
    elif unit == 6:
        priority = 1
    else:
        priority = 2
    reset_time = _number(item.get("nextResetTime"))
    return priority, reset_time if reset_time is not None else math.inf


def _token_label(item: Mapping[str, Any], *, index: int, total: int) -> str:
    unit = _number(item.get("unit"))
    number = _number(item.get("number"))
    if unit == 3 and number == 5:
        return "5 小时"
    if unit == 6:
        return "每周"
    if total == 1 or index == 0:
        return "5 小时"
    if index == 1:
        return "每周"
    return f"Token 周期 {index + 1}"


def _percentage_line(
    label: str,
    item: Mapping[str, Any],
    *,
    local_timezone: tzinfo | None,
    countdown_now: datetime | None = None,
) -> str:
    used = _number(item.get("percentage"))
    if used is None:
        raise QuotaQueryError(f"{label}额度缺少 percentage")
    remaining = min(100.0, max(0.0, 100.0 - used))
    details = [f"已用 {_format_number(used)}%"]
    if countdown_now is not None:
        countdown = _format_reset_countdown(
            item.get("nextResetTime"),
            now=countdown_now,
        )
        if countdown is not None:
            details.append(f"刷新还剩 {countdown}")
    else:
        reset_time = _format_reset_time(item.get("nextResetTime"), local_timezone)
        if reset_time is not None:
            details.append(f"下次刷新：{reset_time}")
    return f"{label}剩余额度：{_format_number(remaining)}%（{'；'.join(details)}）"


def _time_limit_line(
    label: str,
    item: Mapping[str, Any],
    *,
    local_timezone: tzinfo | None,
) -> str:
    remaining = _number(item.get("remaining"))
    total = _number(item.get("usage"))
    used_percentage = _number(item.get("percentage"))
    reset_time = _format_reset_time(item.get("nextResetTime"), local_timezone)

    if remaining is not None and total is not None:
        details: list[str] = []
        if total > 0:
            remaining_percentage = min(100.0, max(0.0, remaining / total * 100.0))
            details.append(f"剩余 {_format_number(remaining_percentage)}%")
        if reset_time is not None:
            details.append(f"下次刷新：{reset_time}")
        suffix = f"（{'；'.join(details)}）" if details else ""
        return (
            f"{label}剩余额度：{_format_number(remaining)} / "
            f"{_format_number(total)}{suffix}"
        )
    if used_percentage is not None:
        return _percentage_line(label, item, local_timezone=local_timezone)
    raise QuotaQueryError(f"{label}额度缺少可用数值")


def _format_reset_countdown(value: Any, *, now: datetime) -> str | None:
    timestamp_ms = _number(value)
    if timestamp_ms is None:
        return None
    remaining_seconds = max(0.0, timestamp_ms / 1000 - now.timestamp())
    total_minutes = math.ceil(remaining_seconds / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def _format_reset_time(value: Any, local_timezone: tzinfo | None) -> str | None:
    timestamp_ms = _number(value)
    if timestamp_ms is None:
        return None
    display_timezone = local_timezone or datetime.now().astimezone().tzinfo
    try:
        reset_at = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=timezone.utc,
        ).astimezone(display_timezone)
    except (OSError, OverflowError, ValueError):
        return None

    offset = reset_at.strftime("%z")
    timezone_suffix = f" UTC{offset[:3]}:{offset[3:]}" if offset else ""
    return reset_at.strftime("%Y-%m-%d %H:%M:%S") + timezone_suffix


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _format_number(value: float) -> str:
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}".rstrip("0").rstrip(".")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查询 GLM Coding Plan 剩余额度")
    parser.add_argument(
        "--platform",
        choices=tuple(ENDPOINTS),
        default="bigmodel",
        help="bigmodel=国内智谱（默认），zai=国际版 Z.AI",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="请求超时秒数（默认：15）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("错误：timeout 必须是大于 0 的有限数字", file=sys.stderr)
        return 2

    api_key = getpass.getpass("GLM Coding Plan API Key（输入不会显示）：")
    try:
        payload = query_quota(
            api_key,
            platform=args.platform,
            timeout=args.timeout,
        )
        for line in format_quota(payload):
            print(line)
    except QuotaQueryError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
