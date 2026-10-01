"""Fast, offline tests: tool surface + structured failure (no hang)."""

from __future__ import annotations

import asyncio
import time

from conftest import DEAD_PORT_ENV, bridge_session, tool_payload

EXPECTED_TOOLS = {"generate_audiobook", "translate_text"}


def test_lists_exactly_the_two_bridge_tools() -> None:
    async def run() -> set[str]:
        async with bridge_session() as session:
            result = await session.list_tools()
        return {t.name for t in result.tools}

    assert asyncio.run(run()) == EXPECTED_TOOLS


def test_translate_dead_port_returns_structured_error_fast() -> None:
    async def run():
        async with bridge_session(DEAD_PORT_ENV) as session:
            return await session.call_tool(
                "translate_text", {"text": "hello", "src": "en", "tgt": "zh"}
            )

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


def test_generate_audiobook_missing_document_is_structured() -> None:
    async def run():
        async with bridge_session(DEAD_PORT_ENV) as session:
            return await session.call_tool(
                "generate_audiobook", {"import_path": "/tmp/does-not-exist-bridge.txt"}
            )

    payload = tool_payload(asyncio.run(run()))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "import_not_found"


def test_generate_audiobook_unreachable_when_document_exists(tmp_path) -> None:
    doc = tmp_path / "mini.txt"
    doc.write_text("Hello there, this is a bridge failure-path probe.")

    async def run():
        async with bridge_session(DEAD_PORT_ENV) as session:
            return await session.call_tool(
                "generate_audiobook",
                {"import_path": str(doc), "voice": "", "language": "en"},
            )

    payload = tool_payload(asyncio.run(run()))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "vs_unreachable"
