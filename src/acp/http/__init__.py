"""HTTP/WebSocket transport for ACP.

This module provides WebSocket-based transport for ACP agents and clients,
enabling communication over HTTP/WebSocket instead of stdio.

Key components:
    - WebSocketStreamAdapter: Bridges WebSocket with asyncio streams
    - connect_http_agent: Connect to remote ACP agent via WebSocket
"""

from .adapter import StarletteWebSocketWrapper, WebSocketStreamAdapter
from .client import connect_http_agent

__all__ = ["StarletteWebSocketWrapper", "WebSocketStreamAdapter", "connect_http_agent"]
