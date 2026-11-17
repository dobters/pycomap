"""Transport layer abstraction for the ComAp native protocol.

``Transport`` is an ``abc.ABC`` — implementations must subclass it and provide the four
async methods. ``EthernetTransport`` is the only implementation for now; AirGate (cloud
relay) and serial transports can be added later without changing ``ComApClient``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from abc import ABC, abstractmethod
from ipaddress import IPv4Address

from pycomap.exceptions import ComApConnectionError

DEFAULT_PORT = 23
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_WRITE_TIMEOUT = 10.0

# TCP keepalive: probe after 30 s idle, every 10 s, drop after 3 misses (~60 s to detect a
# dead peer instead of the OS default of hours).
_KEEPALIVE_IDLE = 30
_KEEPALIVE_INTERVAL = 10
_KEEPALIVE_COUNT = 3

_log = logging.getLogger(__name__)


class Transport(ABC):
    """Byte-stream transport used by ``ComApClient``."""

    @abstractmethod
    async def connect(self) -> None:
        """Open the underlying connection."""

    @abstractmethod
    async def close(self) -> None:
        """Close the underlying connection, suppressing already-closed errors."""

    @abstractmethod
    async def read_exactly(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, raising ``ComApConnectionError`` if the stream ends."""

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write ``data`` and flush."""


class EthernetTransport(Transport):
    """TCP transport for the ComAp native protocol (port 23).

    Connects to ``host:port`` via plain TCP; the ``ComApClient`` layer adds the
    ECDH/AES framing on top.
    """

    def __init__(
        self,
        host: IPv4Address,
        port: int = DEFAULT_PORT,
        *,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float | None = DEFAULT_READ_TIMEOUT,
        write_timeout: float | None = DEFAULT_WRITE_TIMEOUT,
    ) -> None:
        """
        Args:
            host: Controller IP address. Exposed back via
                [host][pycomap.protocol.EthernetTransport.host] for e.g. probing reachability
                via [discover_host][pycomap.discovery.discover_host].
            port: TCP port; defaults to ``23`` (ComAp native protocol port).
            connect_timeout: Seconds to wait for the TCP connection to open (``None`` = no limit).
            read_timeout: Seconds to wait for each ``read_exactly`` call to complete, i.e. for
                the controller to send the requested bytes (``None`` = no limit).
            write_timeout: Seconds to wait for each ``write`` to flush (``None`` = no limit).

        A timeout, like any socket error, raises
        [ComApConnectionError][pycomap.ComApConnectionError] and closes the transport: after a
        timeout the stream may be out of sync with the controller, so it must be reopened with
        ``connect()`` before further use.
        """
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def host(self) -> IPv4Address:
        """The controller's configured IP address."""
        return self._host

    @property
    def port(self) -> int:
        """The controller's configured TCP port."""
        return self._port

    async def connect(self) -> None:
        _log.debug("connecting to %s:%d", self._host, self._port)
        # Drop any previous (possibly half-dead) connection before opening a new one.
        self._abort()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(str(self._host), self._port),
                self._connect_timeout,
            )
        except TimeoutError as exc:
            raise ComApConnectionError(
                f"timed out connecting to {self._host}:{self._port}"
            ) from exc
        except OSError as exc:
            raise ComApConnectionError(f"failed to connect to {self._host}:{self._port}") from exc
        self._enable_keepalive()
        _log.info("connected to %s:%d", self._host, self._port)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self._writer.wait_closed(), self._write_timeout)
            _log.info("disconnected from %s:%d", self._host, self._port)
        self._reader = None
        self._writer = None

    async def read_exactly(self, n: int) -> bytes:
        if self._reader is None:
            raise ComApConnectionError("not connected — call connect() first")
        try:
            return await asyncio.wait_for(self._reader.readexactly(n), self._read_timeout)
        except asyncio.IncompleteReadError as exc:
            self._abort()
            raise ComApConnectionError("connection closed while reading a message") from exc
        except TimeoutError as exc:
            self._abort()
            raise ComApConnectionError(
                f"timed out after {self._read_timeout}s waiting for data from the controller"
            ) from exc
        except OSError as exc:
            self._abort()
            raise ComApConnectionError(f"connection error while reading: {exc}") from exc

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise ComApConnectionError("not connected — call connect() first")
        try:
            self._writer.write(data)
            await asyncio.wait_for(self._writer.drain(), self._write_timeout)
        except TimeoutError as exc:
            self._abort()
            raise ComApConnectionError(
                f"timed out after {self._write_timeout}s writing to the controller"
            ) from exc
        except OSError as exc:
            self._abort()
            raise ComApConnectionError(f"connection error while writing: {exc}") from exc

    def _abort(self) -> None:
        """Drop the connection immediately (no graceful close) and forget the streams."""
        if self._writer is not None:
            with contextlib.suppress(OSError):
                self._writer.transport.abort()
        self._reader = None
        self._writer = None

    def _enable_keepalive(self) -> None:
        """Turn on TCP keepalive so a silently dead peer is detected in about a minute."""
        if self._writer is None:
            return
        sock = self._writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Idle option name differs per platform: TCP_KEEPIDLE (Linux/Windows),
            # TCP_KEEPALIVE (macOS). Interval/count are best-effort.
            idle_opt = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))
            for opt, value in (
                (idle_opt, _KEEPALIVE_IDLE),
                (getattr(socket, "TCP_KEEPINTVL", None), _KEEPALIVE_INTERVAL),
                (getattr(socket, "TCP_KEEPCNT", None), _KEEPALIVE_COUNT),
            ):
                if opt is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            _log.debug("could not configure TCP keepalive", exc_info=True)
