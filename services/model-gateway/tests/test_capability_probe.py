from __future__ import annotations

from pathlib import Path
import sqlite3

import httpx
import pytest

from model_gateway.capability_probe import (
    build_probe_connection,
    build_probe_deployment,
    probe_chat_capabilities,
)
from model_gateway.models import ServerConfig
from model_gateway.storage import StorageFaultMonitor
from model_gateway.usage import UsageStore


@pytest.mark.asyncio
async def test_probe_marks_tools_and_json_when_provider_accepts() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = httpx.Request(
            request.method, str(request.url), content=request.content
        )
        body = payload.read()
        import json

        data = json.loads(body)
        seen.append(data)
        # Accept all probe shapes.
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-probe",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    connection = build_probe_connection(
        channel_operator="vendor",
        base_url="https://vendor.example/v1",
        adapter="generic",
        auth_type="bearer",
        allowed_private_networks=[],
    )
    deployment = build_probe_deployment(
        connection_id="capability-probe",
        upstream_model="vendor-chat",
    )
    result = await probe_chat_capabilities(
        connection=connection,
        deployment=deployment,
        secret="probe-secret-value",
        transport=httpx.MockTransport(handler),
    )
    assert result["ok"] is True
    assert result["capabilities"]["tools"] is True
    assert result["capabilities"]["json_object"] is True
    assert result["capabilities"]["streaming"] is True
    assert any(item.get("tools") for item in seen)
    assert any(
        isinstance(item.get("response_format"), dict)
        and item["response_format"].get("type") == "json_object"
        for item in seen
    )


@pytest.mark.asyncio
async def test_probe_stops_feature_checks_when_chat_fails() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            401,
            json={"error": {"message": "invalid api key"}},
        )

    connection = build_probe_connection(
        channel_operator="vendor",
        base_url="https://vendor.example/v1",
        adapter="generic",
        auth_type="bearer",
        allowed_private_networks=[],
    )
    deployment = build_probe_deployment(
        connection_id="capability-probe",
        upstream_model="vendor-chat",
    )
    result = await probe_chat_capabilities(
        connection=connection,
        deployment=deployment,
        secret="bad-secret",
        transport=httpx.MockTransport(handler),
    )
    assert result["ok"] is False
    assert calls == 1
    assert result["capabilities"]["tools"] is False


@pytest.mark.asyncio
async def test_capability_probes_record_distinct_operations(
    tmp_path: Path,
) -> None:
    connection = build_probe_connection(
        channel_operator="vendor",
        base_url="https://vendor.example/v1",
        adapter="generic",
        auth_type="bearer",
        allowed_private_networks=[],
    )
    deployment = build_probe_deployment(
        connection_id="capability-probe",
        upstream_model="vendor-chat",
    )
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()

    result = await probe_chat_capabilities(
        connection=connection,
        deployment=deployment,
        secret="probe-secret-value",
        probes=("chat", "tools"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        ),
        usage_store=store,
        usage_server=ServerConfig(),
    )

    assert result["ok"] is True
    assert result["usage_ledger_status"] == "complete"
    with sqlite3.connect(store.path) as database:
        rows = database.execute(
            "SELECT route_id, operation, client_id "
            "FROM usage_events ORDER BY route_id"
        ).fetchall()
    assert rows == [
        (
            "capability.probe.chat",
            "capability.probe.chat",
            "modelgw-capability-probe",
        ),
        (
            "capability.probe.tools",
            "capability.probe.tools",
            "modelgw-capability-probe",
        ),
    ]


@pytest.mark.asyncio
async def test_capability_probe_preserves_result_when_ledger_record_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = build_probe_connection(
        channel_operator="vendor",
        base_url="https://vendor.example/v1",
        adapter="generic",
        auth_type="bearer",
        allowed_private_networks=[],
    )
    deployment = build_probe_deployment(
        connection_id="capability-probe",
        upstream_model="vendor-chat",
    )
    store = UsageStore(tmp_path / "usage.db")
    store.init_db()
    monitor = StorageFaultMonitor()

    def fail_record(**_kwargs: object) -> str:
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(store, "record", fail_record)
    result = await probe_chat_capabilities(
        connection=connection,
        deployment=deployment,
        secret="probe-secret-value",
        probes=("chat",),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        ),
        usage_store=store,
        usage_server=ServerConfig(),
        storage_monitor=monitor,
    )

    assert result["ok"] is True
    assert result["usage_ledger_status"] == "incomplete"
    assert result["details"]["chat"]["ok"] is True
    assert result["warnings"]
    assert monitor.consume_after_successful_probe() == "disk_unavailable"
