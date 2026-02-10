"""Tests for acp.http WebSocket transport adapter and client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from acp import (
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    text_block,
    update_agent_message,
)
from acp.agent.connection import AgentSideConnection
from acp.http.adapter import (
    StarletteWebSocketWrapper,
    WebSocketStreamAdapter,
)
from acp.schema import (
    AudioContentBlock,
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    ResourceContentBlock,
    SseMcpServer,
    TextContentBlock,
)

from tests.conftest import TestAgent, TestClient


# --------------- Helpers ---------------


class FakeWebSocket:
    """In-memory WebSocket-like object for testing."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._outgoing: list[str | bytes] = []
        self._closed = False

    async def recv(self) -> str | bytes:
        msg = await self._incoming.get()
        if msg is None:
            raise ConnectionError("WebSocket closed")
        return msg

    async def send(self, data: str | bytes) -> None:
        self._outgoing.append(data)

    async def close(self) -> None:
        self._closed = True

    def inject(self, message: str | bytes) -> None:
        """Inject a message as if received from the remote peer."""
        self._incoming.put_nowait(message)

    def inject_close(self) -> None:
        """Signal that the WebSocket has closed."""
        self._incoming.put_nowait(None)


# --------------- WebSocketStreamAdapter tests ---------------


@pytest.mark.asyncio
async def test_adapter_start_creates_reader_writer() -> None:
    """After start(), reader and writer should be available."""
    ws = FakeWebSocket()
    adapter = WebSocketStreamAdapter(ws)
    await adapter.start()

    assert adapter.reader is not None
    assert adapter.writer is not None

    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_writer_raises_before_start() -> None:
    """Accessing writer before start() should raise RuntimeError."""
    ws = FakeWebSocket()
    adapter = WebSocketStreamAdapter(ws)

    with pytest.raises(RuntimeError, match="not started"):
        _ = adapter.writer


@pytest.mark.asyncio
async def test_adapter_double_start_raises() -> None:
    """Calling start() twice should raise RuntimeError."""
    ws = FakeWebSocket()
    adapter = WebSocketStreamAdapter(ws)
    await adapter.start()

    with pytest.raises(RuntimeError, match="already started"):
        await adapter.start()

    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_receive_feeds_reader() -> None:
    """Messages from WebSocket should be readable from StreamReader."""
    ws = FakeWebSocket()
    adapter = WebSocketStreamAdapter(ws)
    await adapter.start()

    # Inject a message into the fake WebSocket
    ws.inject("hello world\n")
    # Give receive loop time to process
    await asyncio.sleep(0.05)

    line = await asyncio.wait_for(adapter.reader.readline(), timeout=1.0)
    assert line == b"hello world\n"

    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_receive_bytes() -> None:
    """Byte messages from WebSocket should also be fed to reader."""
    ws = FakeWebSocket()
    adapter = WebSocketStreamAdapter(ws)
    await adapter.start()

    ws.inject(b"binary data\n")
    await asyncio.sleep(0.05)

    line = await asyncio.wait_for(adapter.reader.readline(), timeout=1.0)
    assert line == b"binary data\n"

    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_send_loop() -> None:
    """Data written to StreamWriter should be sent via WebSocket."""
    ws = FakeWebSocket()
    adapter = WebSocketStreamAdapter(ws)
    await adapter.start()

    adapter.writer.write(b"outgoing message")
    # Give send loop time to drain
    await asyncio.sleep(0.1)

    assert len(ws._outgoing) == 1
    assert ws._outgoing[0] == "outgoing message"

    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_close_is_idempotent() -> None:
    """Calling close() multiple times should not raise."""
    ws = FakeWebSocket()
    adapter = WebSocketStreamAdapter(ws)
    await adapter.start()

    await adapter.close()
    await adapter.close()  # Should not raise

    assert ws._closed


@pytest.mark.asyncio
async def test_adapter_eof_on_ws_disconnect() -> None:
    """When WebSocket disconnects, reader should get EOF."""
    ws = FakeWebSocket()
    adapter = WebSocketStreamAdapter(ws)
    await adapter.start()

    ws.inject_close()
    await asyncio.sleep(0.05)

    data = await asyncio.wait_for(adapter.reader.read(1024), timeout=1.0)
    assert data == b""  # EOF

    await adapter.close()


# --------------- StarletteWebSocketWrapper tests ---------------


@pytest.mark.asyncio
async def test_starlette_wrapper_recv() -> None:
    """Wrapper should delegate recv to receive_text."""
    mock_ws = AsyncMock()
    mock_ws.receive_text.return_value = "hello"

    wrapper = StarletteWebSocketWrapper(mock_ws)
    result = await wrapper.recv()

    assert result == "hello"
    mock_ws.receive_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_starlette_wrapper_send_text() -> None:
    """Wrapper should delegate send to send_text."""
    mock_ws = AsyncMock()
    wrapper = StarletteWebSocketWrapper(mock_ws)

    await wrapper.send("hello")
    mock_ws.send_text.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_starlette_wrapper_send_bytes() -> None:
    """Wrapper should convert bytes to text before sending."""
    mock_ws = AsyncMock()
    wrapper = StarletteWebSocketWrapper(mock_ws)

    await wrapper.send(b"binary")
    mock_ws.send_text.assert_awaited_once_with("binary")


@pytest.mark.asyncio
async def test_starlette_wrapper_close() -> None:
    """Wrapper should delegate close."""
    mock_ws = AsyncMock()
    wrapper = StarletteWebSocketWrapper(mock_ws)

    await wrapper.close()
    mock_ws.close.assert_awaited_once()


# --------------- End-to-end: Agent + Client over WebSocket adapter ---------------


@pytest.mark.asyncio
async def test_e2e_agent_client_over_websocket_adapter() -> None:
    """Full round-trip: client connects to agent through WebSocket adapters."""
    agent = TestAgent()
    client = TestClient()

    # Create a pair of in-memory WebSockets that are cross-connected
    agent_ws = FakeWebSocket()
    client_ws = FakeWebSocket()

    agent_adapter = WebSocketStreamAdapter(agent_ws)
    client_adapter = WebSocketStreamAdapter(client_ws)

    await agent_adapter.start()
    await client_adapter.start()

    # Cross-wire: agent's outgoing → client's incoming, and vice versa
    # We'll do this by running relay tasks
    async def relay(src_adapter: WebSocketStreamAdapter, dst_ws: FakeWebSocket) -> None:
        """Read from one adapter's WebSocket outgoing and inject into the other."""
        while True:
            await asyncio.sleep(0.01)
            while src_adapter._ws._outgoing:  # type: ignore[attr-defined]
                msg = src_adapter._ws._outgoing.pop(0)  # type: ignore[attr-defined]
                dst_ws.inject(msg if isinstance(msg, str) else msg.decode("utf-8"))

    relay1 = asyncio.create_task(relay(agent_adapter, client_ws))
    relay2 = asyncio.create_task(relay(client_adapter, agent_ws))

    try:
        # Create connections
        from acp.core import AgentSideConnection, ClientSideConnection

        agent_conn = AgentSideConnection(
            agent, agent_adapter.writer, agent_adapter.reader, listening=True
        )
        client_conn = ClientSideConnection(
            client, client_adapter.writer, client_adapter.reader
        )

        # Initialize
        init_resp = await asyncio.wait_for(
            client_conn.initialize(
                protocol_version=1,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="test", title="Test", version="0.1"),
            ),
            timeout=5.0,
        )
        assert init_resp.protocol_version == 1

        # Create session
        session = await asyncio.wait_for(
            client_conn.new_session(mcp_servers=[], cwd="/tmp"),
            timeout=5.0,
        )
        assert session.session_id == "test-session-123"

        # Send prompt
        resp = await asyncio.wait_for(
            client_conn.prompt(
                session_id=session.session_id,
                prompt=[text_block("Hello via WebSocket!")],
            ),
            timeout=5.0,
        )
        assert resp.stop_reason == "end_turn"
        assert len(agent.prompts) == 1

    finally:
        relay1.cancel()
        relay2.cancel()
        await agent_adapter.close()
        await client_adapter.close()
