from __future__ import annotations

import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from scripts import glm_coding_plan_quota as quota


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def read(self, amount: int | None = None) -> bytes:
        assert amount == quota.MAX_RESPONSE_BYTES + 1
        return self.body


class FakeConnection:
    def __init__(self, status: int, body: bytes) -> None:
        self.response = FakeResponse(status, body)
        self.request_call: tuple[str, str, Mapping[str, str] | None] | None = None
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        assert body is None
        self.request_call = (method, url, headers)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class GlmCodingPlanQuotaTests(unittest.TestCase):
    def test_query_quota_uses_official_endpoint_and_exact_api_key(self) -> None:
        connection = FakeConnection(
            200,
            b'{"code":200,"success":true,"data":{"limits":[]}}',
        )
        factory_call: dict[str, Any] = {}

        def factory(host: str, port: int, *, timeout: float) -> FakeConnection:
            factory_call.update(host=host, port=port, timeout=timeout)
            return connection

        payload = quota.query_quota(
            "  test-key  ",
            timeout=3.5,
            connection_factory=factory,
        )

        self.assertIs(payload["success"], True)
        self.assertEqual(
            factory_call,
            {
                "host": "open.bigmodel.cn",
                "port": 443,
                "timeout": 3.5,
            },
        )
        self.assertIsNotNone(connection.request_call)
        assert connection.request_call is not None
        method, path, headers = connection.request_call
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/api/monitor/usage/quota/limit")
        self.assertIsNotNone(headers)
        assert headers is not None
        self.assertEqual(headers["Authorization"], "test-key")
        self.assertIs(connection.closed, True)

    def test_format_quota_shows_five_hour_countdown_and_other_reset_times(
        self,
    ) -> None:
        china_timezone = timezone(timedelta(hours=8))
        now = datetime(2026, 8, 16, 8, 17, 10, tzinfo=timezone.utc)
        five_hour_reset = int(
            datetime(2026, 8, 16, 10, 32, 5, tzinfo=timezone.utc).timestamp()
            * 1000
        )
        weekly_reset = int(
            datetime(2026, 8, 20, 1, 15, 0, tzinfo=timezone.utc).timestamp()
            * 1000
        )
        payload = {
            "data": {
                "limits": [
                    {
                        "type": "TIME_LIMIT",
                        "percentage": 7.2,
                        "usage": 1000,
                        "currentValue": 72,
                        "remaining": 928,
                    },
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 6,
                        "number": 1,
                        "percentage": 53,
                        "nextResetTime": weekly_reset,
                    },
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 3,
                        "number": 5,
                        "percentage": 44,
                        "nextResetTime": five_hour_reset,
                    },
                ]
            }
        }

        self.assertEqual(
            quota.format_quota(
                payload,
                local_timezone=china_timezone,
                now=now,
            ),
            [
                "5 小时剩余额度：56%（已用 44%；刷新还剩 2 小时 15 分钟）",
                "每周剩余额度：47%（已用 53%；下次刷新：2026-08-20 09:15:00 UTC+08:00）",
                "MCP 月度剩余额度：928 / 1,000（剩余 92.8%）",
            ],
        )

    def test_five_hour_countdown_rounds_partial_minute_up(self) -> None:
        now = datetime(2026, 8, 16, 10, 31, 6, tzinfo=timezone.utc)
        reset_time = int(
            datetime(2026, 8, 16, 10, 32, 5, tzinfo=timezone.utc).timestamp()
            * 1000
        )
        payload = {
            "data": {
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "unit": 3,
                        "number": 5,
                        "percentage": 44,
                        "nextResetTime": reset_time,
                    }
                ]
            }
        }

        self.assertEqual(
            quota.format_quota(payload, now=now),
            ["5 小时剩余额度：56%（已用 44%；刷新还剩 1 分钟）"],
        )

    def test_format_quota_displays_mcp_reset_when_returned(self) -> None:
        reset_time = int(
            datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        payload = {
            "data": {
                "limits": [
                    {
                        "type": "TIME_LIMIT",
                        "percentage": 25,
                        "usage": 1000,
                        "currentValue": 250,
                        "remaining": 750,
                        "nextResetTime": reset_time,
                    }
                ]
            }
        }

        self.assertEqual(
            quota.format_quota(payload, local_timezone=timezone.utc),
            [
                "MCP 月度剩余额度：750 / 1,000"
                "（剩余 75%；下次刷新：2026-09-01 00:00:00 UTC+00:00）"
            ],
        )

    def test_query_quota_reports_rejected_key_without_exposing_it(self) -> None:
        connection = FakeConnection(
            401,
            '{"code":401,"msg":"访问令牌无效"}'.encode(),
        )

        with self.assertRaises(quota.QuotaQueryError) as raised:
            quota.query_quota(
                "secret-that-must-not-leak",
                connection_factory=lambda *args, **kwargs: connection,
            )

        message = str(raised.exception)
        self.assertIn("API Key 无效或无权查询额度", message)
        self.assertNotIn("secret-that-must-not-leak", message)
        self.assertIs(connection.closed, True)

    def test_query_quota_rejects_invalid_key_before_connecting(self) -> None:
        def unexpected_factory(*args: Any, **kwargs: Any) -> FakeConnection:
            raise AssertionError("invalid key must not trigger a network request")

        for api_key in ("", "  ", "abc\r\nforged: value"):
            with self.subTest(api_key=api_key):
                with self.assertRaises(quota.QuotaQueryError):
                    quota.query_quota(
                        api_key,
                        connection_factory=unexpected_factory,
                    )

    def test_format_quota_rejects_missing_limits(self) -> None:
        with self.assertRaisesRegex(quota.QuotaQueryError, "limits"):
            quota.format_quota({"data": {}})


if __name__ == "__main__":
    unittest.main()
