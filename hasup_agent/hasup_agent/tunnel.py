"""Tunnel client side (W4.6).

The server multiplexes TCP streams of a Home Assistant UI session over the agent
WebSocket. For each ``tunnel_open`` the agent opens a TCP connection to the local Home
Assistant instance (``http://homeassistant.local.hass.io:8123`` by default) and pipes
bytes both ways as base64 ``tunnel_data`` frames, one increasing ``seq`` per stream and
per direction.

Back-pressure: reading from the local socket is gated by a bounded outbound window
(``MAX_INFLIGHT_CHUNKS``), and frames coming from the server are queued per stream with
a bounded queue; a stream whose queue stays full is closed rather than letting memory
grow. Frames arriving out of order are buffered up to the 64-message window required by
``docs/protocol.md`` section 7.8, beyond which the stream is closed with ``error``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from hasup_protocol import (
    TunnelClosedMessage,
    TunnelClosedPayload,
    TunnelCloseMessage,
    TunnelCloseReason,
    TunnelDataMessage,
    TunnelDataPayload,
    TunnelOpenMessage,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 32 * 1024
MAX_INFLIGHT_CHUNKS = 32
REORDER_WINDOW = 64
CONNECT_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 300.0

SendCallable = Callable[[Any], Awaitable[None]]


class TunnelStream:
    """One proxied TCP connection."""

    def __init__(
        self,
        stream_id: uuid.UUID,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        send: SendCallable,
        on_closed: Callable[[uuid.UUID], None],
    ) -> None:
        self.stream_id = stream_id
        self._reader = reader
        self._writer = writer
        self._send = send
        self._on_closed = on_closed
        self._out_seq = 0
        self._next_in_seq = 0
        self._pending: dict[int, bytes] = {}
        self._inflight = asyncio.Semaphore(MAX_INFLIGHT_CHUNKS)
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        reason = TunnelCloseReason.TARGET_CLOSED
        try:
            while True:
                try:
                    async with asyncio.timeout(IDLE_TIMEOUT_S):
                        chunk = await self._reader.read(CHUNK_SIZE)
                except TimeoutError:
                    reason = TunnelCloseReason.TIMEOUT
                    break
                if not chunk:
                    break
                # Back-pressure: never keep more than MAX_INFLIGHT_CHUNKS chunks in
                # flight towards the relay.
                await self._inflight.acquire()
                try:
                    await self._send(
                        TunnelDataMessage(
                            payload=TunnelDataPayload(
                                stream_id=self.stream_id,
                                seq=self._out_seq,
                                data=base64.b64encode(chunk).decode("ascii"),
                            )
                        )
                    )
                finally:
                    self._inflight.release()
                self._out_seq += 1
        except (OSError, asyncio.IncompleteReadError) as exc:
            logger.warning("tunnel %s read error: %s", self.stream_id, exc)
            reason = TunnelCloseReason.ERROR
        except asyncio.CancelledError:
            raise
        finally:
            await self.close(reason, notify=True)

    async def feed(self, seq: int, data: bytes) -> None:
        """Write a server chunk to the local socket, reordering if needed."""
        if self._closed:
            return
        if seq < self._next_in_seq:
            logger.debug("tunnel %s: duplicate seq %d ignored", self.stream_id, seq)
            return
        self._pending[seq] = data
        if len(self._pending) > REORDER_WINDOW:
            logger.error(
                "tunnel %s: more than %d out-of-order frames, closing",
                self.stream_id,
                REORDER_WINDOW,
            )
            await self.close(TunnelCloseReason.ERROR, notify=True)
            return
        while (chunk := self._pending.pop(self._next_in_seq, None)) is not None:
            self._writer.write(chunk)
            self._next_in_seq += 1
        await self._writer.drain()

    async def close(self, reason: TunnelCloseReason, *, notify: bool) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError, RuntimeError):
            self._writer.close()
        with contextlib.suppress(OSError, RuntimeError, asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(self._writer.wait_closed(), timeout=5.0)
        self._on_closed(self.stream_id)
        if notify:
            await self._send(
                TunnelClosedMessage(
                    payload=TunnelClosedPayload(stream_id=self.stream_id, reason=reason)
                )
            )

    async def cancel(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
        await self.close(TunnelCloseReason.CLIENT_CLOSED, notify=False)


class TunnelClient:
    """Handles the tunnel messages of one relay session."""

    def __init__(self, target: str, send: SendCallable, *, enabled: bool = True) -> None:
        self._target = target
        self._send = send
        self._enabled = enabled
        self._streams: dict[uuid.UUID, TunnelStream] = {}

    @property
    def streams(self) -> dict[uuid.UUID, TunnelStream]:
        return self._streams

    async def handle(self, message: Any) -> None:
        if isinstance(message, TunnelOpenMessage):
            await self._open(message)
        elif isinstance(message, TunnelDataMessage):
            await self._data(message)
        elif isinstance(message, TunnelCloseMessage):
            await self._close(message)

    async def _open(self, message: TunnelOpenMessage) -> None:
        stream_id = message.payload.stream_id
        if not self._enabled:
            logger.warning("tunnel disabled by configuration, refusing stream %s", stream_id)
            await self._notify_closed(stream_id, TunnelCloseReason.ERROR)
            return
        if stream_id in self._streams:
            logger.warning("tunnel %s already open", stream_id)
            return

        # The target advertised by the server is informative; the agent connects to the
        # configured local Home Assistant, never to an arbitrary address.
        host, port = _host_port(self._target)
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                reader, writer = await asyncio.open_connection(host, port)
        except (OSError, TimeoutError) as exc:
            logger.error("tunnel %s: cannot reach %s:%s (%s)", stream_id, host, port, exc)
            await self._notify_closed(stream_id, TunnelCloseReason.ERROR)
            return

        stream = TunnelStream(stream_id, reader, writer, self._send, self._forget)
        self._streams[stream_id] = stream
        stream.start()
        logger.info("tunnel %s opened to %s:%s", stream_id, host, port)

    async def _data(self, message: TunnelDataMessage) -> None:
        stream = self._streams.get(message.payload.stream_id)
        if stream is None:
            logger.debug("tunnel data for unknown stream %s", message.payload.stream_id)
            return
        try:
            data = base64.b64decode(message.payload.data, validate=True)
        except (ValueError, TypeError):
            logger.error("tunnel %s: invalid base64 payload", message.payload.stream_id)
            await stream.close(TunnelCloseReason.ERROR, notify=True)
            return
        await stream.feed(message.payload.seq, data)

    async def _close(self, message: TunnelCloseMessage) -> None:
        stream = self._streams.get(message.payload.stream_id)
        if stream is None:
            return
        await stream.cancel()

    async def close_all(self) -> None:
        for stream in list(self._streams.values()):
            await stream.cancel()
        self._streams.clear()

    def _forget(self, stream_id: uuid.UUID) -> None:
        self._streams.pop(stream_id, None)

    async def _notify_closed(self, stream_id: uuid.UUID, reason: TunnelCloseReason) -> None:
        await self._send(
            TunnelClosedMessage(payload=TunnelClosedPayload(stream_id=stream_id, reason=reason))
        )


def _host_port(target: str) -> tuple[str, int]:
    parsed = urlparse(target if "://" in target else f"http://{target}")
    host = parsed.hostname or "homeassistant.local.hass.io"
    port = parsed.port or (443 if parsed.scheme == "https" else 8123)
    return host, port
