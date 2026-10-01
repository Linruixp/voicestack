"""Shared stdio MCP client harness for voicebridge tests."""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def bridge_session(
    env_overrides: dict[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    """Spawn voicebridge over stdio with a merged environment and initialise it."""
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "voicebridge"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def tool_payload(result: Any) -> dict[str, Any]:
    """Extract the JSON dict returned by a voicebridge tool call."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return json.loads(result.content[0].text)


# Dead-port env: skip the launcher so the failure is an immediate reachability
# error rather than a 90 s cold-start attempt.
DEAD_PORT_ENV = {
    "VS_BASE_URL": "http://127.0.0.1:3999",
    "VS_ENSURE_UP_ENABLED": "0",
    "VS_HEALTH_TIMEOUT": "3",
    "VS_TRANSLATE_TIMEOUT": "5",
}
