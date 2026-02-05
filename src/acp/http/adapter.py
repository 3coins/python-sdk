"""WebSocket to asyncio streams adapter.

This module provides adapters to bridge WebSocket connections with asyncio's
StreamReader/StreamWriter interfaces, enabling the transport-agnostic Connection
class to work over WebSocket.

The adapter uses a 4-queue architecture:
1. InMemoryMessageQueue (ACP internal) - incoming messages after parsing
2. MessageSender._queue (ACP internal) - outgoing messages before sending
3. WebSocketStreamAdapter._incoming - bridge from WebSocket to StreamReader
4. WebSocketStreamAdapter._outgoing - bridge from StreamWriter to WebSocket

Two background tasks pump messages through the bridge queues to coordinate
between WebSocket's async API and the streaming interfaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from typing import Any, Protocol

from acp.task.supervisor import TaskSupervisor

__all__ = ["WebSocketLike", "StarletteWebSocketWrapper", "WebSocketStreamAdapter"]

logger = logging.getLogger(__name__)


class WebSocketLike(Protocol):
    """Protocol for WebSocket-like objects.

    This allows the adapter to work with any WebSocket implementation that
    provides these basic methods (websockets, Starlette, etc.).
    """

    async def recv(self) -> str | bytes:
        """Receive a message from the WebSocket."""
        ...

    async def send(self, data: str | bytes) -> None:
        """Send a message to the WebSocket."""
        ...

    async def close(self) -> None:
        """Close the WebSocket connection."""
        ...


class StarletteWebSocketWrapper:
    """Wrapper for Starlette WebSocket to match WebSocketLike protocol.

    Starlette uses receive_text()/send_text() while websockets library
    uses recv()/send(). This wrapper provides a consistent interface.
    """

    def __init__(self, websocket: Any) -> None:
        """Initialize wrapper with Starlette WebSocket.

        Args:
            websocket: Starlette WebSocket instance.
        """
        self._ws = websocket

    async def recv(self) -> str | bytes:
        """Receive message from Starlette WebSocket."""
        return await self._ws.receive_text()

    async def send(self, data: str | bytes) -> None:
        """Send message to Starlette WebSocket."""
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        await self._ws.send_text(data)

    async def close(self) -> None:
        """Close Starlette WebSocket."""
        await self._ws.close()


class _WebSocketTransport(asyncio.Transport):
    """Custom asyncio.Transport that writes to a synchronous buffer.

    This transport is used with asyncio.StreamWriter to bridge the writer
    with our WebSocket send loop. It uses a deque for instant, synchronous writes
    and an Event to signal when data is available.
    """

    def __init__(self, buffer: deque[bytes], data_event: asyncio.Event, protocol: asyncio.Protocol) -> None:
        super().__init__()
        self._buffer = buffer
        self._data_event = data_event
        self._protocol = protocol
        self._closing = False

    def write(self, data: bytes) -> None:
        """Write data to the buffer (synchronous, instant).

        Args:
            data: Bytes to send via WebSocket.
        """
        if self._closing:
            return
        # Add to buffer instantly (synchronous)
        self._buffer.append(data)
        # Signal the send loop that data is available
        self._data_event.set()

    def close(self) -> None:
        """Close the transport."""
        if not self._closing:
            self._closing = True
            # Signal send loop to wake up and check closing status
            self._data_event.set()

    def is_closing(self) -> bool:
        """Check if transport is closing."""
        return self._closing

    def get_protocol(self) -> asyncio.Protocol:
        """Get the protocol."""
        return self._protocol


class WebSocketStreamAdapter:
    """Bridges WebSocket with asyncio StreamReader/StreamWriter.

    This adapter manages two queues and two background tasks to enable
    bidirectional message flow between a WebSocket connection and the
    stream-based ACP Connection class.

    Message flow:
        INCOMING: WebSocket.recv() → _incoming queue → StreamReader.readline()
        OUTGOING: StreamWriter.write() → _outgoing queue → WebSocket.send()

    Usage:
        adapter = WebSocketStreamAdapter(websocket)
        await adapter.start()

        # Use reader/writer with Connection
        conn = Connection(handler=..., reader=adapter.reader, writer=adapter.writer)

        # Clean up
        await adapter.close()
    """

    def __init__(self, websocket: WebSocketLike) -> None:
        """Initialize adapter with a WebSocket connection.

        Args:
            websocket: WebSocket-like object with recv(), send(), close() methods.
        """
        self._ws = websocket

        # Synchronous buffer for outgoing messages
        self._outgoing: deque[bytes] = deque()

        # Event to signal when data is available (created in start())
        self._data_event: asyncio.Event | None = None

        # Create actual asyncio.StreamReader
        self._reader = asyncio.StreamReader()

        # StreamWriter will be created in start() when we have the running loop
        self._writer: asyncio.StreamWriter | None = None

        # Background task management
        self._recv_task: asyncio.Task[Any] | None = None
        self._send_task: asyncio.Task[Any] | None = None
        self._supervisor = TaskSupervisor(source="http.WebSocketAdapter")
        self._closed = False

    async def start(self) -> None:
        """Start background tasks to pump messages through queues.

        Must be called before using reader/writer interfaces.
        """
        if self._recv_task is not None or self._send_task is not None:
            raise RuntimeError("Adapter already started")

        # Create Event with the running event loop
        self._data_event = asyncio.Event()

        # Create StreamWriter with the running event loop
        loop = asyncio.get_running_loop()
        protocol = asyncio.StreamReaderProtocol(self._reader, loop=loop)
        transport = _WebSocketTransport(self._outgoing, self._data_event, protocol)
        self._writer = asyncio.StreamWriter(transport, protocol, self._reader, loop)

        self._recv_task = self._supervisor.create(
            self._receive_loop(),
            name="websocket.receive",
            on_error=self._on_receive_error,
        )
        self._send_task = self._supervisor.create(
            self._send_loop(),
            name="websocket.send",
            on_error=self._on_send_error,
        )

    async def _receive_loop(self) -> None:
        """Pump messages: WebSocket → StreamReader via feed_data()."""
        try:
            while not self._closed:
                # Receive message from WebSocket
                data = await self._ws.recv()

                # Convert to bytes if needed
                if isinstance(data, str):
                    data = data.encode("utf-8")

                # Feed data directly to StreamReader
                self._reader.feed_data(data)
        except Exception as exc:
            # WebSocket disconnected or error
            logger.debug("WebSocket receive loop ended: %s", exc)
        finally:
            # Signal EOF to StreamReader
            self._reader.feed_eof()

    async def _send_loop(self) -> None:
        """Pump messages: StreamWriter → outgoing buffer → WebSocket.

        This loop waits on an event signal, then drains all available data
        from the buffer and sends it over WebSocket.
        """
        if self._data_event is None:
            raise RuntimeError("Data event not initialized - start() must be called first")

        try:
            while not self._closed:
                # Wait for data to be available or close signal
                await self._data_event.wait()

                # Clear the event for next iteration
                self._data_event.clear()

                # Drain all available data from buffer
                while self._outgoing:
                    data = self._outgoing.popleft()
                    if not data:  # EOF signal (empty bytes)
                        return

                    # Send as WebSocket message (text frame)
                    text = data.decode("utf-8")
                    await self._ws.send(text)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("WebSocket send loop ended: %s", exc)

    def _on_receive_error(self, task: asyncio.Task[Any], exc: BaseException) -> None:
        """Handle receive loop errors."""
        if not isinstance(exc, asyncio.CancelledError):
            logger.exception("WebSocket receive loop failed", exc_info=exc)

    def _on_send_error(self, task: asyncio.Task[Any], exc: BaseException) -> None:
        """Handle send loop errors."""
        if not isinstance(exc, asyncio.CancelledError):
            logger.exception("WebSocket send loop failed", exc_info=exc)

    async def close(self) -> None:
        """Shutdown background tasks and close WebSocket.

        Performs graceful shutdown in correct order:
        1. Mark as closed to stop loops
        2. Signal EOF to send loop
        3. Shutdown background tasks
        4. Close WebSocket connection
        """
        if self._closed:
            return

        self._closed = True

        # Signal EOF to send loop (empty bytes in buffer)
        self._outgoing.append(b"")
        if self._data_event is not None:
            self._data_event.set()

        # Shutdown background tasks
        await self._supervisor.shutdown()

        # Close WebSocket connection
        with contextlib.suppress(Exception):
            await self._ws.close()

    @property
    def reader(self) -> asyncio.StreamReader:
        """Get the asyncio StreamReader."""
        return self._reader

    @property
    def writer(self) -> asyncio.StreamWriter:
        """Get the asyncio StreamWriter.

        Raises:
            RuntimeError: If start() hasn't been called yet.
        """
        if self._writer is None:
            raise RuntimeError("Adapter not started - call start() first")
        return self._writer
