# HTTP/WebSocket Transport

The `acp.http` module provides a WebSocket-based transport layer for ACP, enabling agents and clients to communicate over HTTP instead of stdio. This is useful for deploying agents as remote web services.

## Overview

While stdio transport works well for local agents spawned as child processes, many deployment scenarios require network-based communication:

- Agents running on cloud infrastructure (e.g., AWS Bedrock AgentCore)
- Multiple clients connecting to a shared agent service
- Agents behind load balancers or API gateways

The HTTP/WebSocket transport uses the same `Connection` API as stdio, so existing agent and client code works without changes.

## Architecture

The adapter uses a 4-queue architecture to bridge WebSocket messages with asyncio streams:

```
INCOMING: WebSocket.recv() → StreamReader.feed_data() → ACP Connection
OUTGOING: ACP Connection → StreamWriter.write() → deque buffer → WebSocket.send()
```

Two background tasks pump messages through the bridge:

- **Receive loop**: Reads from WebSocket, feeds bytes to `asyncio.StreamReader`
- **Send loop**: Drains the outgoing `deque` buffer and sends via WebSocket

This design allows `AgentSideConnection` and `ClientSideConnection` to work unchanged — they read/write asyncio streams as usual.

## Components

### `WebSocketStreamAdapter`

Bridges any WebSocket connection with asyncio `StreamReader`/`StreamWriter`:

```python
from acp.http import WebSocketStreamAdapter

adapter = WebSocketStreamAdapter(websocket)
await adapter.start()

# Use adapter.reader and adapter.writer with ACP Connection
conn = AgentSideConnection(agent, adapter.writer, adapter.reader)

# Clean up
await adapter.close()
```

### `connect_http_agent`

Async context manager for client-side WebSocket connections:

```python
from acp.http import connect_http_agent

async with connect_http_agent(client, "ws://localhost:8080/ws") as conn:
    await conn.initialize(protocol_version=PROTOCOL_VERSION)
    session = await conn.new_session(mcp_servers=[], cwd=".")
    await conn.prompt(session_id=session.session_id, prompt=[text_block("Hello")])
```

Accepts optional `**ws_kwargs` passed through to `websockets.connect()` (e.g., `max_size`, `extra_headers`, `compression`).

### `WebSocketLike` Protocol

Any object implementing `recv()`, `send()`, and `close()` works with the adapter:

```python
class WebSocketLike(Protocol):
    async def recv(self) -> str | bytes: ...
    async def send(self, data: str | bytes) -> None: ...
    async def close(self) -> None: ...
```

### `StarletteWebSocketWrapper`

Pre-built wrapper for Starlette's WebSocket (which uses `receive_text()`/`send_text()` instead of `recv()`/`send()`):

```python
from acp.http import StarletteWebSocketWrapper

wrapped = StarletteWebSocketWrapper(starlette_websocket)
adapter = WebSocketStreamAdapter(wrapped)
```

## Server-Side Example

Serve an ACP agent over WebSocket using Starlette:

```python
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from acp.agent.connection import AgentSideConnection
from acp.http import StarletteWebSocketWrapper, WebSocketStreamAdapter

async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    wrapped = StarletteWebSocketWrapper(websocket)
    adapter = WebSocketStreamAdapter(wrapped)

    await adapter.start()

    agent = MyAgent()
    conn = AgentSideConnection(
        to_agent=agent,
        input_stream=adapter.writer,
        output_stream=adapter.reader,
        listening=False,
    )

    await conn.listen()
    await adapter.close()

app = Starlette(routes=[WebSocketRoute("/ws", websocket_endpoint)])
```

See `examples/http_echo_agent.py` for a complete runnable server.

## Client-Side Example

Connect to a remote agent:

```python
from acp.http import connect_http_agent
from acp import PROTOCOL_VERSION, text_block
from acp.schema import ClientCapabilities, Implementation

async with connect_http_agent(MyClient(), "ws://agent.example.com/ws") as conn:
    await conn.initialize(
        protocol_version=PROTOCOL_VERSION,
        client_capabilities=ClientCapabilities(),
        client_info=Implementation(name="my-client", title="My Client", version="0.1.0"),
    )
    session = await conn.new_session(mcp_servers=[], cwd="/workspace")
    await conn.prompt(session_id=session.session_id, prompt=[text_block("Hello!")])
```

See `examples/http_client.py` for a complete runnable client with interactive prompt.

## Dependencies

- **`websockets>=12.0`** — Required (core dependency). Used by `connect_http_agent` for client-side connections.
- **`starlette`** — Optional, for server-side `StarletteWebSocketWrapper`. Install separately: `pip install starlette uvicorn`.

## Limitations

- Messages use newline-delimited JSON format (one JSON-RPC message per line), consistent with stdio transport.
- Default maximum WebSocket message size is 50MB (matching stdio buffer limit). Override via `max_size` kwarg.
- The `StarletteWebSocketWrapper` currently handles text frames only. Binary WebSocket frames are converted to UTF-8.
