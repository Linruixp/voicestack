"""vsbridge MCP server: on-demand VoiceStudio ensure-up + live MCP proxy (stdio).

The server is a *proxy*, not a reimplementation:

* ``tools/list`` — forwarded to VoiceStudio when it is already up (and the result
  cached); otherwise the live-derived catalog answers, so listing never
  cold-starts the app.
* ``tools/call`` — runs ``vs-ensure-up.sh`` first, then forwards the call to
  VoiceStudio's MCP ``/mcp/`` and returns its result verbatim. Every failure is a
  structured JSON payload — never a hang.

Speaks **stdio only** — never binds a socket.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any

from anyio import to_thread
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .catalog import ToolCatalog
from .config import BridgeConfig
from .ensure import EnsureUp
from .errors import BridgeError
from .mcp_client import VoiceStudioMcpClient

SERVER_NAME = "vsbridge"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "VoiceStudio on-demand MCP bridge. VoiceStudio runs on-demand: each tool "
    "call starts it via vs-ensure-up.sh and waits for :3900 before proxying to "
    "VoiceStudio's own MCP. Use it for speech generation, voice cloning and "
    "transcription. Audiobook generation and translation live on the voicebridge "
    "server, not here."
)


def _error_result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        ],
        is_error=True,
    )


def build_server(config: BridgeConfig | None = None) -> Server:
    cfg = config or BridgeConfig.from_env()
    client = VoiceStudioMcpClient(cfg)
    ensurer = EnsureUp(cfg)
    catalog = ToolCatalog(cfg.catalog_path)

    async def on_list_tools(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        # Mirror the live server when it is already up; never cold-start here.
        try:
            if await to_thread.run_sync(client.probe):
                tools = await to_thread.run_sync(client.list_tools)
                await to_thread.run_sync(catalog.save, tools)
                return types.ListToolsResult(
                    tools=[types.Tool.model_validate(t) for t in tools]
                )
        except BridgeError:
            pass
        try:
            fallback = await to_thread.run_sync(catalog.load)
        except BridgeError:
            fallback = []
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(t) for t in fallback]
        )

    async def on_call_tool(
        _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        try:
            await to_thread.run_sync(ensurer.ensure_up)
            result = await to_thread.run_sync(
                partial(client.call_tool, name, arguments)
            )
            return types.CallToolResult.model_validate(result)
        except BridgeError as exc:
            return _error_result(exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - boundary safety net, must not hang
            failure = BridgeError(
                "unexpected_error", f"{exc.__class__.__name__}: {exc}"
            )
            return _error_result(failure.to_dict())

    return Server(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def _serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
