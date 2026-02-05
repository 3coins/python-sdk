"""HTTP/WebSocket client example for ACP.

This example shows how to connect to an ACP agent over HTTP/WebSocket using
the connect_http_agent function. This is the client-side counterpart to
http_echo_agent.py.

Requirements:
    pip install agent-client-protocol

Usage:
    # Start the server first (in another terminal):
    python examples/http_echo_agent.py

    # Then run the client:
    python examples/http_client.py ws://localhost:8080/ws
"""

import asyncio
import logging
import sys
from typing import Any

from acp import (
    PROTOCOL_VERSION,
    Client,
    RequestError,
    text_block,
)
from acp.http import connect_http_agent
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AudioContentBlock,
    AvailableCommandsUpdate,
    ClientCapabilities,
    CreateTerminalResponse,
    CurrentModeUpdate,
    EmbeddedResourceContentBlock,
    EnvVariable,
    ImageContentBlock,
    Implementation,
    KillTerminalCommandResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    ResourceContentBlock,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCall,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ExampleClient(Client):
    """Example client implementation with minimal method stubs."""

    async def request_permission(
        self, options: list[PermissionOption], session_id: str, tool_call: ToolCall, **kwargs: Any
    ) -> RequestPermissionResponse:
        raise RequestError.method_not_found("session/request_permission")

    async def write_text_file(
        self, content: str, path: str, session_id: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(
        self, path: str, session_id: str, limit: int | None = None, line: int | None = None, **kwargs: Any
    ) -> ReadTextFileResponse:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalCommandResponse | None:
        raise RequestError.method_not_found("terminal/kill")

    async def session_update(
        self,
        session_id: str,
        update: UserMessageChunk
        | AgentMessageChunk
        | AgentThoughtChunk
        | ToolCallStart
        | ToolCallProgress
        | AgentPlanUpdate
        | AvailableCommandsUpdate
        | CurrentModeUpdate,
        **kwargs: Any,
    ) -> None:
        """Handle session updates from the agent."""
        if not isinstance(update, AgentMessageChunk):
            return

        content = update.content
        text: str
        if isinstance(content, TextContentBlock):
            text = content.text
        elif isinstance(content, ImageContentBlock):
            text = "<image>"
        elif isinstance(content, AudioContentBlock):
            text = "<audio>"
        elif isinstance(content, ResourceContentBlock):
            text = content.uri or "<resource>"
        elif isinstance(content, EmbeddedResourceContentBlock):
            text = "<resource>"
        else:
            text = "<content>"

        print(f"| Agent: {text}")

    async def ext_method(self, method: str, params: dict) -> dict:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict) -> None:
        raise RequestError.method_not_found(method)


async def read_console(prompt: str) -> str:
    """Read input from console asynchronously."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


async def interactive_loop(conn, session_id: str) -> None:
    """Run interactive prompt loop."""
    print("\nConnected to agent. Type messages (Ctrl+C or Ctrl+D to exit):\n")

    while True:
        try:
            line = await read_console("> ")
        except EOFError:
            break
        except KeyboardInterrupt:
            print("", file=sys.stderr)
            break

        if not line:
            continue

        try:
            await conn.prompt(
                session_id=session_id,
                prompt=[text_block(line)],
            )
        except Exception as exc:
            logger.error("Prompt failed: %s", exc)


async def main(argv: list[str]) -> int:
    """Main entry point."""
    if len(argv) < 2:
        print("Usage: python examples/http_client.py WS_URL", file=sys.stderr)
        print("Example: python examples/http_client.py ws://localhost:8080/ws", file=sys.stderr)
        return 2

    url = argv[1]
    logger.info("Connecting to %s", url)

    # Connect to agent via WebSocket
    async with connect_http_agent(ExampleClient(), url) as conn:
        logger.info("Connected! Initializing protocol...")

        # Initialize protocol
        await conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="http-client", title="HTTP Client Example", version="0.1.0"),
        )

        # Create new session
        import os

        session = await conn.new_session(mcp_servers=[], cwd=os.getcwd())
        logger.info("Session created: %s", session.session_id)

        # Run interactive loop
        await interactive_loop(conn, session.session_id)

    logger.info("Connection closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
