"""HTTP/WebSocket server example for ACP echo agent.

This example shows how to serve an ACP agent over HTTP/WebSocket using
Starlette and the WebSocketStreamAdapter. This is useful for deploying
agents as web services that can be accessed remotely.

The example implements a simple echo agent that reflects back any messages
it receives from clients.

Architecture:
    - Starlette handles HTTP server and WebSocket connections
    - WebSocketStreamAdapter bridges WebSocket with asyncio streams
    - Each WebSocket connection gets its own EchoAgent instance
    - 4-queue architecture: WS → Queue → Reader → ACP Queue → Handler → ACP Queue → Writer → Queue → WS

Requirements:
    pip install agent-client-protocol starlette uvicorn

Usage:
    python examples/http_echo_agent.py

Then connect a client:
    python examples/http_client.py ws://localhost:8080/ws
"""

import asyncio
import logging
from typing import Any
from uuid import uuid4

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from acp import (
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    text_block,
    update_agent_message,
)
from acp.agent.connection import AgentSideConnection
from acp.http import StarletteWebSocketWrapper, WebSocketStreamAdapter
from acp.interfaces import Client
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class EchoAgent(Agent):
    """Simple echo agent that reflects back messages."""

    _conn: Client

    def on_connect(self, conn: Client) -> None:
        """Store client connection reference."""
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        """Initialize protocol with client."""
        logger.info("Agent initialized with protocol version %d", protocol_version)
        return InitializeResponse(protocol_version=protocol_version)

    async def new_session(
        self, cwd: str, mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio], **kwargs: Any
    ) -> NewSessionResponse:
        """Create a new session."""
        session_id = uuid4().hex
        logger.info("Created new session: %s", session_id)
        return NewSessionResponse(session_id=session_id)

    async def prompt(
        self,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        session_id: str,
        **kwargs: Any,
    ) -> PromptResponse:
        """Echo back the prompt."""
        logger.info("Received prompt with %d blocks", len(prompt))

        for block in prompt:
            # Extract text from block
            text = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")

            # Send echo response as agent message
            chunk = update_agent_message(text_block(text))
            chunk.field_meta = {"echo": True}
            chunk.content.field_meta = {"echo": True}

            await self._conn.session_update(session_id=session_id, update=chunk, source="http_echo_agent")

        return PromptResponse(stop_reason="end_turn")


async def websocket_endpoint(websocket: WebSocket) -> None:
    """Handle WebSocket connections for ACP protocol.

    Each WebSocket connection gets its own agent instance and runs through
    the full connection lifecycle:
    1. Accept WebSocket connection
    2. Create adapter to bridge WebSocket with asyncio streams
    3. Start background tasks to pump messages through queues
    4. Create AgentSideConnection and listen for messages
    5. Clean up when connection closes
    """
    await websocket.accept()
    logger.info("WebSocket connection accepted from %s", websocket.client)

    # Wrap Starlette WebSocket to match WebSocketLike protocol
    wrapped_ws = StarletteWebSocketWrapper(websocket)

    # Create adapter to bridge WebSocket with asyncio streams
    adapter = WebSocketStreamAdapter(wrapped_ws)

    try:
        # Start background tasks for bidirectional message pumping
        await adapter.start()
        logger.debug("WebSocket adapter started")

        # Create agent instance for this connection
        agent = EchoAgent()

        # Create AgentSideConnection directly
        # From agent's perspective:
        # - input_stream (writer) = for sending to client
        # - output_stream (reader) = for receiving from client
        # Use listening=False so the receive loop doesn't auto-start
        # (it will be started by listen() call below)
        agent_conn = AgentSideConnection(
            to_agent=agent,
            input_stream=adapter.writer,  # Agent sends via this
            output_stream=adapter.reader,  # Agent receives via this
            listening=False,  # Don't auto-start receive loop
        )

        logger.info("Agent connection established, starting message loop")

        # Run main message loop (blocks until connection closes)
        await agent_conn.listen()

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception:
        logger.exception("WebSocket handler error")
    finally:
        # Clean up in correct order: connection first, then adapter
        if 'agent_conn' in locals():
            await agent_conn.close()
        await adapter.close()
        logger.info("WebSocket connection closed")


# Create Starlette app with WebSocket endpoint
app = Starlette(
    debug=True,
    routes=[
        WebSocketRoute("/ws", websocket_endpoint),
    ],
)


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting ACP echo agent server on ws://localhost:8080/ws")

    # Run server with uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )
