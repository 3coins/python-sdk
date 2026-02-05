"""HTTP/WebSocket client transport for ACP.

This module provides client-side connection functions for connecting to
ACP agents over HTTP/WebSocket.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import websockets

from acp.client.connection import ClientSideConnection
from acp.interfaces import Agent, Client

from .adapter import WebSocketStreamAdapter

__all__ = ["connect_http_agent"]


@asynccontextmanager
async def connect_http_agent(
    to_client: Callable[[Agent], Client] | Client,
    url: str,
    **ws_kwargs: Any,
) -> AsyncIterator[ClientSideConnection]:
    """Connect to an ACP agent via HTTP/WebSocket.

    This function establishes a WebSocket connection to an ACP agent server
    and returns a ClientSideConnection ready for protocol communication.

    Args:
        to_client: Client implementation or factory function that takes an Agent
            and returns a Client instance.
        url: WebSocket URL (e.g., "ws://localhost:8080/ws" or "wss://example.com/acp").
        **ws_kwargs: Additional keyword arguments passed to websockets.connect()
            (e.g., max_size, compression, extra_headers).

    Yields:
        ClientSideConnection: Ready-to-use connection for making protocol calls
            like initialize(), new_session(), prompt(), etc.

    Example:
        ```python
        from acp.http import connect_http_agent
        from acp.meta import PROTOCOL_VERSION

        async with connect_http_agent(
            ExampleClient(),
            "ws://localhost:8080/ws",
        ) as conn:
            # Initialize protocol
            await conn.initialize(protocol_version=PROTOCOL_VERSION)

            # Create session
            session = await conn.new_session()

            # Send prompt
            await conn.prompt(
                session_id=session.session_id,
                prompt=[{"type": "text", "text": "Hello!"}],
            )
        ```

    Notes:
        - WebSocket messages use newline-delimited JSON format (one JSON-RPC message per line)
        - Large messages (>50MB) are supported via WebSocket message fragmentation
        - Connection is automatically closed when exiting the async context manager
    """
    # Set default max_size to 50MB (matches stdio buffer limit) if not specified
    if "max_size" not in ws_kwargs:
        ws_kwargs["max_size"] = 50 * 1024 * 1024

    # Connect to WebSocket
    async with websockets.connect(url, **ws_kwargs) as websocket:
        # Create adapter to bridge WebSocket with asyncio streams
        adapter = WebSocketStreamAdapter(websocket)
        await adapter.start()

        conn = None
        try:
            # Create ClientSideConnection
            # Note: ClientSideConnection expects (to_client, writer, reader)
            # - writer (input_stream) = for sending to agent
            # - reader (output_stream) = for receiving from agent
            conn = ClientSideConnection(to_client, adapter.writer, adapter.reader)

            yield conn

        finally:
            # Clean up adapter and connection
            await adapter.close()
            if conn is not None:
                await conn.close()
