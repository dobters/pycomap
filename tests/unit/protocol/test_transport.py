import asyncio
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from ipaddress import IPv4Address

import pytest

from pycomap.exceptions import ComApConnectionError
from pycomap.protocol.transport import EthernetTransport, Transport


@pytest.fixture
def transport() -> EthernetTransport:
    return EthernetTransport(IPv4Address("192.168.1.9"))


def test_transport_subclass_missing_methods_cannot_be_instantiated() -> None:
    class Incomplete(Transport):
        async def connect(self) -> None:
            pass

    with pytest.raises(TypeError, match="abstract"):
        Incomplete()  # ty: ignore[abstract-class-instantiation]


async def test_read_exactly_before_connect_raises_connection_error(
    transport: EthernetTransport,
) -> None:
    with pytest.raises(ComApConnectionError, match="not connected"):
        await transport.read_exactly(1)


async def test_write_before_connect_raises_connection_error(
    transport: EthernetTransport,
) -> None:
    with pytest.raises(ComApConnectionError, match="not connected"):
        await transport.write(b"\x00")


async def test_read_exactly_after_close_raises_connection_error(
    transport: EthernetTransport,
) -> None:
    await transport.close()  # closing without ever connecting is a no-op
    with pytest.raises(ComApConnectionError, match="not connected"):
        await transport.read_exactly(1)


async def test_write_after_close_raises_connection_error(transport: EthernetTransport) -> None:
    await transport.close()
    with pytest.raises(ComApConnectionError, match="not connected"):
        await transport.write(b"\x00")


# -- timeouts, socket errors and reconnect ---------------------------------------------------


@asynccontextmanager
async def _server(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncGenerator[int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        server.close_clients()  # handlers may still be parked; don't wait for them
        await server.wait_closed()


async def _silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await asyncio.sleep(30)


async def test_read_timeout_raises_connection_error_and_drops_connection() -> None:
    async with _server(_silent) as port:
        t = EthernetTransport(IPv4Address("127.0.0.1"), port, read_timeout=0.1)
        await t.connect()
        with pytest.raises(ComApConnectionError, match="timed out"):
            await t.read_exactly(1)
        # the stream may be out of sync after a timeout, so it must not be reusable
        with pytest.raises(ComApConnectionError, match="not connected"):
            await t.read_exactly(1)


async def test_peer_closing_connection_raises_connection_error() -> None:
    async def close_immediately(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    async with _server(close_immediately) as port:
        t = EthernetTransport(IPv4Address("127.0.0.1"), port)
        await t.connect()
        with pytest.raises(ComApConnectionError, match="closed"):
            await t.read_exactly(1)


async def test_connection_reset_during_read_is_wrapped() -> None:
    t = EthernetTransport(IPv4Address("127.0.0.1"), 1)
    reader = asyncio.StreamReader()
    reader.set_exception(ConnectionResetError("reset by peer"))
    t._reader = reader
    with pytest.raises(ComApConnectionError, match="reset by peer"):
        await t.read_exactly(1)
    assert t._reader is None


async def test_write_oserror_is_wrapped_and_drops_connection() -> None:
    class BrokenWriter:
        transport = type("T", (), {"abort": lambda self: None})()

        def write(self, data: bytes) -> None:
            raise BrokenPipeError("broken pipe")

    t = EthernetTransport(IPv4Address("127.0.0.1"), 1)
    t._writer = BrokenWriter()  # ty: ignore[invalid-assignment]
    with pytest.raises(ComApConnectionError, match="broken pipe"):
        await t.write(b"x")
    with pytest.raises(ComApConnectionError, match="not connected"):
        await t.write(b"x")


async def test_connect_timeout_raises_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(*_: object, **__: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(asyncio, "open_connection", hang)
    t = EthernetTransport(IPv4Address("192.0.2.1"), connect_timeout=0.1)
    with pytest.raises(ComApConnectionError, match="timed out connecting"):
        await t.connect()


async def test_reconnect_after_timeout_works() -> None:
    async def echo_one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"ok")
        await writer.drain()
        await asyncio.sleep(30)

    async with _server(echo_one) as port:
        t = EthernetTransport(IPv4Address("127.0.0.1"), port, read_timeout=0.2)
        await t.connect()
        assert await t.read_exactly(2) == b"ok"
        with pytest.raises(ComApConnectionError, match="timed out"):
            await t.read_exactly(1)

        await t.connect()  # fresh session on the same transport object
        assert await t.read_exactly(2) == b"ok"
        await t.close()


async def test_tcp_keepalive_enabled_on_connect() -> None:
    async with _server(_silent) as port:
        t = EthernetTransport(IPv4Address("127.0.0.1"), port)
        await t.connect()
        assert t._writer is not None
        sock = t._writer.get_extra_info("socket")
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        await t.close()


async def test_none_timeouts_disable_limits() -> None:
    t = EthernetTransport(
        IPv4Address("127.0.0.1"), connect_timeout=None, read_timeout=None, write_timeout=None
    )
    assert t._read_timeout is None
