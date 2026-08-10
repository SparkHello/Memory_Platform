from __future__ import annotations

import asyncio
import contextlib


LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 2026
TARGET_HOST = "memory-gateway"
TARGET_PORT = 2026
MAX_CONNECTIONS = 128
MAX_CONNECTIONS_PER_SOURCE = 32
CONNECT_TIMEOUT_SECONDS = 5.0
FIRST_BYTE_TIMEOUT_SECONDS = 10.0
IDLE_TIMEOUT_SECONDS = 1800.0
IO_TIMEOUT_SECONDS = 30.0
BUFFER_LIMIT_BYTES = 64 * 1024
CHUNK_BYTES = 64 * 1024


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def _half_close(writer: asyncio.StreamWriter) -> None:
    if writer.can_write_eof():
        with contextlib.suppress(Exception):
            writer.write_eof()
            await asyncio.wait_for(writer.drain(), timeout=IO_TIMEOUT_SECONDS)


async def _copy(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    idle_timeout: float,
    first_timeout: float | None = None,
) -> None:
    timeout = idle_timeout if first_timeout is None else first_timeout
    while True:
        data = await asyncio.wait_for(reader.read(CHUNK_BYTES), timeout=timeout)
        if not data:
            await _half_close(writer)
            return
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout=IO_TIMEOUT_SECONDS)
        timeout = idle_timeout


class FixedTargetRelay:
    """Bounded raw TCP relay with no HTTP parsing or payload logging."""

    def __init__(
        self,
        *,
        target_host: str = TARGET_HOST,
        target_port: int = TARGET_PORT,
        max_connections: int = MAX_CONNECTIONS,
        max_connections_per_source: int = MAX_CONNECTIONS_PER_SOURCE,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        first_byte_timeout: float | None = FIRST_BYTE_TIMEOUT_SECONDS,
    ) -> None:
        if max_connections < 1:
            raise ValueError("max_connections")
        if max_connections_per_source < 1:
            raise ValueError("max_connections_per_source")
        self._target_host = target_host
        self._target_port = target_port
        self._max_connections = max_connections
        self._max_connections_per_source = max_connections_per_source
        self._connect_timeout = connect_timeout
        self._idle_timeout = idle_timeout
        self._first_byte_timeout = first_byte_timeout
        self._active_connections = 0
        self._per_source_counts: dict[str, int] = {}

    async def handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        # The event loop runs handlers cooperatively, so these checks and
        # increments are atomic until the first await.
        if self._active_connections >= self._max_connections:
            await _close(client_writer)
            return
        peername = client_writer.get_extra_info("peername")
        source = peername[0] if peername else ""
        per_source = self._per_source_counts.get(source, 0)
        if per_source >= self._max_connections_per_source:
            await _close(client_writer)
            return
        self._active_connections += 1
        self._per_source_counts[source] = per_source + 1
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._target_host,
                    self._target_port,
                    limit=BUFFER_LIMIT_BYTES,
                ),
                timeout=self._connect_timeout,
            )
            downstream = asyncio.create_task(
                _copy(
                    client_reader,
                    upstream_writer,
                    idle_timeout=self._idle_timeout,
                    first_timeout=self._first_byte_timeout,
                )
            )
            upstream = asyncio.create_task(
                _copy(
                    upstream_reader,
                    client_writer,
                    idle_timeout=self._idle_timeout,
                )
            )
            try:
                await asyncio.gather(downstream, upstream)
            finally:
                for task in (downstream, upstream):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(downstream, upstream, return_exceptions=True)
        except (OSError, asyncio.TimeoutError):
            # Deliberately do not log addresses, bytes, headers or exceptions.
            pass
        finally:
            if upstream_writer is not None:
                await _close(upstream_writer)
            await _close(client_writer)
            self._active_connections -= 1
            remaining = self._per_source_counts.get(source, 1) - 1
            if remaining > 0:
                self._per_source_counts[source] = remaining
            else:
                self._per_source_counts.pop(source, None)


async def serve(
    *,
    listen_host: str = LISTEN_HOST,
    listen_port: int = LISTEN_PORT,
    target_host: str = TARGET_HOST,
    target_port: int = TARGET_PORT,
    max_connections: int = MAX_CONNECTIONS,
    max_connections_per_source: int = MAX_CONNECTIONS_PER_SOURCE,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    first_byte_timeout: float | None = FIRST_BYTE_TIMEOUT_SECONDS,
) -> None:
    relay = FixedTargetRelay(
        target_host=target_host,
        target_port=target_port,
        max_connections=max_connections,
        max_connections_per_source=max_connections_per_source,
        connect_timeout=connect_timeout,
        idle_timeout=idle_timeout,
        first_byte_timeout=first_byte_timeout,
    )
    server = await asyncio.start_server(
        relay.handle,
        listen_host,
        listen_port,
        limit=BUFFER_LIMIT_BYTES,
        backlog=max_connections,
        start_serving=True,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
