"""Fast, offline tests: proxied tool surface + structured failure (no hang)."""

from __future__ import annotations

import asyncio
import time

from conftest import DEAD_PORT_ENV, bridge_session, tool_payload

LIVE_TOOLS = {
    "generate_speech",
    "list_voices",
    "list_personalities",
    "list_languages",
    "transcribe",
    "check_health",
    "clone_voice",
}


def test_lists_live_tool_surface_from_catalog_when_down() -> None:
    """tools/list mirrors the real server's tools even with :3900 down."""

    async def run() -> set[str]:
        async with bridge_session(DEAD_PORT_ENV) as session:
            result = await session.list_tools()
        return {t.name for t in result.tools}

    assert asyncio.run(run()) == LIVE_TOOLS


def test_tools_list_does_not_cold_start_and_is_fast() -> None:
    """Listing with the launcher ENABLED but the port dead must not hang."""

    async def run() -> set[str]:
        async with bridge_session(
            {"VS_BASE_URL": "http://127.0.0.1:3998", "VS_PROBE_TIMEOUT": "0.2"}
        ) as session:
            result = await session.list_tools()
        return {t.name for t in result.tools}

    started = time.monotonic()
    names = asyncio.run(run())
    elapsed = time.monotonic() - started
    assert names == LIVE_TOOLS
    assert elapsed < 15, f"tools/list took {elapsed:.1f}s (should not cold-start)"


def test_check_health_dead_port_returns_structured_error_fast() -> None:
    async def run():
        async with bridge_session(DEAD_PORT_ENV) as session:
            return await session.call_tool("check_health", {})

    started = time.monotonic()
    result = asyncio.run(run())
    elapsed = time.monotonic() - started
    payload = tool_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "vs_unreachable"
    assert payload["error"]["message"]
    assert elapsed < 20, (
        f"dead-port failure took {elapsed:.1f}s (structured, not a hang)"
    )


def test_generate_speech_dead_port_returns_structured_error() -> None:
    async def run():
        async with bridge_session(DEAD_PORT_ENV) as session:
            return await session.call_tool(
                "generate_speech", {"text": "hello", "language": "en"}
            )

    payload = tool_payload(asyncio.run(run()))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "vs_unreachable"
