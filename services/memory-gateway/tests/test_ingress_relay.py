from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load_relay():
    path = ROOT / "deploy" / "ingress_relay.py"
    spec = importlib.util.spec_from_file_location("memory_platform_ingress_relay", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_relay_preserves_raw_bytes_and_both_half_closes() -> None:
    module = _load_relay()
    received: list[bytes] = []

    async def target(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        data = await reader.read()
        received.append(data)
        writer.write(b"reply:\x00" + data[::-1])
        await writer.drain()
        writer.write_eof()
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    target_server = await asyncio.start_server(target, "127.0.0.1", 0)
    target_port = target_server.sockets[0].getsockname()[1]
    relay = module.FixedTargetRelay(
        target_host="127.0.0.1",
        target_port=target_port,
        idle_timeout=2.0,
    )
    relay_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    relay_port = relay_server.sockets[0].getsockname()[1]
    payload = b"POST /v1/chat HTTP/1.1\r\nX-Test: opaque\r\n\r\n\x00\xffbody"
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        response = await asyncio.wait_for(reader.read(), timeout=3)
        assert response == b"reply:\x00" + payload[::-1]
        assert received == [payload]
        writer.close()
        await writer.wait_closed()
    finally:
        relay_server.close()
        target_server.close()
        await relay_server.wait_closed()
        await target_server.wait_closed()


@pytest.mark.asyncio
async def test_relay_rejects_connections_above_the_fixed_limit() -> None:
    module = _load_relay()
    target_connected = asyncio.Event()
    release_target = asyncio.Event()

    async def target(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        target_connected.set()
        await release_target.wait()
        writer.close()
        await writer.wait_closed()

    target_server = await asyncio.start_server(target, "127.0.0.1", 0)
    target_port = target_server.sockets[0].getsockname()[1]
    relay = module.FixedTargetRelay(
        target_host="127.0.0.1",
        target_port=target_port,
        max_connections=1,
        idle_timeout=2.0,
    )
    relay_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    relay_port = relay_server.sockets[0].getsockname()[1]
    try:
        first_reader, first_writer = await asyncio.open_connection(
            "127.0.0.1", relay_port
        )
        await asyncio.wait_for(target_connected.wait(), timeout=2)
        second_reader, second_writer = await asyncio.open_connection(
            "127.0.0.1", relay_port
        )
        assert await asyncio.wait_for(second_reader.read(1), timeout=2) == b""
        second_writer.close()
        await second_writer.wait_closed()
        release_target.set()
        assert await asyncio.wait_for(first_reader.read(1), timeout=2) == b""
        first_writer.close()
        await first_writer.wait_closed()
    finally:
        release_target.set()
        relay_server.close()
        target_server.close()
        await relay_server.wait_closed()
        await target_server.wait_closed()


async def _echo_target(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    while True:
        data = await reader.read(1024)
        if not data:
            break
        writer.write(data)
        await writer.drain()
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_relay_closes_connection_without_first_byte_and_frees_slot() -> None:
    module = _load_relay()

    target_server = await asyncio.start_server(_echo_target, "127.0.0.1", 0)
    target_port = target_server.sockets[0].getsockname()[1]
    relay = module.FixedTargetRelay(
        target_host="127.0.0.1",
        target_port=target_port,
        max_connections=1,
        idle_timeout=2.0,
        first_byte_timeout=0.05,
    )
    relay_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    relay_port = relay_server.sockets[0].getsockname()[1]
    try:
        idle_reader, idle_writer = await asyncio.open_connection(
            "127.0.0.1", relay_port
        )
        assert await asyncio.wait_for(idle_reader.read(1), timeout=2) == b""
        idle_writer.close()
        await idle_writer.wait_closed()
        # The timed-out connection must have released its slot.
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(4), timeout=2) == b"ping"
        writer.close()
        await writer.wait_closed()
    finally:
        relay_server.close()
        target_server.close()
        await relay_server.wait_closed()
        await target_server.wait_closed()


@pytest.mark.asyncio
async def test_relay_first_byte_timeout_applies_only_to_first_read() -> None:
    module = _load_relay()

    target_server = await asyncio.start_server(_echo_target, "127.0.0.1", 0)
    target_port = target_server.sockets[0].getsockname()[1]
    relay = module.FixedTargetRelay(
        target_host="127.0.0.1",
        target_port=target_port,
        idle_timeout=2.0,
        first_byte_timeout=0.05,
    )
    relay_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    relay_port = relay_server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"a")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(1), timeout=2) == b"a"
        # Past the first byte the longer idle timeout governs the connection.
        await asyncio.sleep(0.2)
        writer.write(b"b")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(1), timeout=2) == b"b"
        writer.close()
        await writer.wait_closed()
    finally:
        relay_server.close()
        target_server.close()
        await relay_server.wait_closed()
        await target_server.wait_closed()


@pytest.mark.asyncio
async def test_relay_rejects_connections_above_the_per_source_limit() -> None:
    module = _load_relay()
    target_connections = 0
    two_connected = asyncio.Event()
    third_connected = asyncio.Event()

    async def target(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal target_connections
        target_connections += 1
        if target_connections == 2:
            two_connected.set()
        if target_connections == 3:
            third_connected.set()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    target_server = await asyncio.start_server(target, "127.0.0.1", 0)
    target_port = target_server.sockets[0].getsockname()[1]
    relay = module.FixedTargetRelay(
        target_host="127.0.0.1",
        target_port=target_port,
        max_connections_per_source=2,
        idle_timeout=2.0,
    )
    relay_server = await asyncio.start_server(relay.handle, "127.0.0.1", 0)
    relay_port = relay_server.sockets[0].getsockname()[1]
    try:
        first_reader, first_writer = await asyncio.open_connection(
            "127.0.0.1", relay_port
        )
        second_reader, second_writer = await asyncio.open_connection(
            "127.0.0.1", relay_port
        )
        await asyncio.wait_for(two_connected.wait(), timeout=2)
        # All test connections share the 127.0.0.1 source bucket.
        third_reader, third_writer = await asyncio.open_connection(
            "127.0.0.1", relay_port
        )
        assert await asyncio.wait_for(third_reader.read(1), timeout=2) == b""
        third_writer.close()
        await third_writer.wait_closed()
        # Closing one connection frees a per-source slot for the next one.
        # The relay decrements its count while tearing the old connection
        # down, so retry until the slot is actually available again.
        first_writer.close()
        await first_writer.wait_closed()
        fourth_reader = fourth_writer = None
        deadline = asyncio.get_running_loop().time() + 2
        while fourth_writer is None:
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail("per-source slot was not released")
            reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
            try:
                await asyncio.wait_for(third_connected.wait(), timeout=0.1)
                fourth_reader, fourth_writer = reader, writer
            except asyncio.TimeoutError:
                writer.close()
                await writer.wait_closed()
        second_writer.close()
        await second_writer.wait_closed()
        fourth_writer.close()
        await fourth_writer.wait_closed()
    finally:
        relay_server.close()
        target_server.close()
        await relay_server.wait_closed()
        await target_server.wait_closed()
