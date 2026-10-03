"""Fast, offline tests for the speaker-service stdio MCP proxy (task 26).

Given: the MCP server spawned over stdio with the launcher disabled or a dead
port, or a call whose file argument does not exist.
When: tools are listed, or a tool is called.
Then: exactly the nine tools are advertised, and failures are structured MCP
errors (``is_error`` + JSON code) returned promptly - never a hang.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

# pytest is configured with ``pythonpath = ["."]`` (pyproject.toml), so the
# service root is already on sys.path; the module under test imports directly.
import mcp_server  # noqa: E402
from mcp_proxy import BridgeError  # noqa: E402

SERVER = str(Path(__file__).resolve().parent.parent / "mcp_server.py")

EXPECTED_TOOLS = {
    "transcribe_meeting",
    "list_speakers",
    "enroll_speaker",
    "attach_to_speaker",
    "identify_speaker",
    "get_meeting",
    "rename_speaker",
    "rename_meeting",
    "open_speaker_ui",
}

# Dead-port env: skip the launcher so the failure is an immediate reachability
# error rather than a cold-start attempt.
DEAD_PORT_ENV = {
    "VASTACK_BASE_URL": "http://127.0.0.1:3998",
    "VASTACK_ENSURE_UP_ENABLED": "0",
    "VASTACK_HEALTH_TIMEOUT": "2",
    "VASTACK_REQUEST_TIMEOUT": "3",
    "VASTACK_TOKEN": "test-token",
}


@asynccontextmanager
async def mcp_session(
    overrides: dict[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    env = dict(os.environ)
    if overrides:
        env.update(overrides)
    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env=env,
        cwd=str(Path(SERVER).parent),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def error_payload(result: Any) -> dict[str, Any]:
    """Assert an MCP error result and decode its structured JSON payload."""
    assert result.is_error is True, f"expected is_error, got: {result.content[0].text}"
    return json.loads(result.content[0].text)


def test_tools_list_returns_the_expected_tools() -> None:
    async def run() -> set[str]:
        async with mcp_session() as session:
            result = await session.list_tools()
        return {tool.name for tool in result.tools}

    assert asyncio.run(run()) == EXPECTED_TOOLS


def test_missing_file_is_a_structured_error_before_any_service_call() -> None:
    async def run() -> Any:
        async with mcp_session() as session:
            return await session.call_tool(
                "transcribe_meeting", {"file_path": "/no/such/meeting.wav"}
            )

    started = time.monotonic()
    payload = error_payload(asyncio.run(run()))
    elapsed = time.monotonic() - started
    assert payload["ok"] is False
    assert payload["error"]["code"] == "file_not_found"
    assert elapsed < 15, (
        f"missing-file error took {elapsed:.1f}s (should not cold-start)"
    )


def test_dead_port_is_a_structured_error_not_a_hang() -> None:
    async def run() -> Any:
        async with mcp_session(DEAD_PORT_ENV) as session:
            return await session.call_tool("list_speakers", {})

    started = time.monotonic()
    payload = error_payload(asyncio.run(run()))
    elapsed = time.monotonic() - started
    assert payload["ok"] is False
    assert payload["error"]["code"] == "service_unreachable"
    assert elapsed < 20, f"dead-port error took {elapsed:.1f}s (structured, not a hang)"


class FakeCtx:
    def __init__(self) -> None:
        self.progress: list[tuple] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress.append((progress, total, message))


def test_progress_heartbeat_is_emitted_while_call_runs() -> None:
    def slow_call() -> dict[str, str]:
        time.sleep(0.35)
        return {"transcript": "ok"}

    async def run() -> tuple[FakeCtx, Any]:
        ctx = FakeCtx()
        result = await mcp_server._run_with_progress(slow_call, ctx, interval=0.1)
        return ctx, result

    ctx, result = asyncio.run(run())
    assert result.is_error is not True
    assert json.loads(result.content[0].text) == {"transcript": "ok"}
    assert len(ctx.progress) >= 3


def test_progress_helper_maps_bridge_error_to_structured_error() -> None:
    def failing_call() -> None:
        raise BridgeError("boom", "nope")

    async def run() -> Any:
        return await mcp_server._run_with_progress(
            failing_call, FakeCtx(), interval=0.1
        )

    result = asyncio.run(run())
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"]["code"] == "boom"


def test_progress_helper_swallows_progress_errors() -> None:
    class ExplodingCtx:
        async def report_progress(
            self,
            progress: float,
            total: float | None = None,
            message: str | None = None,
        ) -> None:
            raise RuntimeError("client sent no progress token")

    def slow_call() -> dict[str, str]:
        time.sleep(0.25)
        return {"transcript": "ok"}

    async def run() -> Any:
        return await mcp_server._run_with_progress(
            slow_call, ExplodingCtx(), interval=0.1
        )

    result = asyncio.run(run())
    assert result.is_error is not True
    assert json.loads(result.content[0].text) == {"transcript": "ok"}
