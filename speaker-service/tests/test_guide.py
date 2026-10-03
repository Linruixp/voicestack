"""Doctest-style guard for ``docs/USAGE.md`` (task 33).

The user guide pins four MCP server keys and 23 tool names. This test fails the
moment any of them drifts from the RUNNING setup, so a stale name in the guide
cannot survive a test run.

What it asserts:

1. The guide's machine-readable manifest (between the ``MCP-TOOL-MANIFEST``
   markers) names exactly the servers registered in
   ``~/.config/opencode/opencode.json``.
2. For every server, the manifest's tool set equals the server's live
   ``tools/list`` obtained by spawning it over stdio.
3. Every live tool name appears in the guide prose too, not only the manifest.
4. Neither the service bearer token nor the Hugging Face token (both read from
   the Keychain) appears anywhere in the guide.

Spawning all four servers is the point of the check. For a fast, offline-only
run set ``VS_GUIDE_SKIP_LIVE=1``; the manifest/config comparison still runs, the
live comparison is skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[2]
GUIDE = REPO / "docs" / "USAGE.md"
OPENCODE_JSON = Path("/Users/LinRui/.config/opencode/opencode.json")

MANIFEST_START = "<!-- MCP-TOOL-MANIFEST"
MANIFEST_END = "<!-- /MCP-TOOL-MANIFEST -->"

# server key -> (keychain service, keychain account) for the secret-absence check
KEYCHAIN_SECRETS: dict[str, tuple[str, str]] = {
    "-service-token": ("voicestack-service", "service"),
    "hf-token": ("voicestack-hf", "voicestack"),
}


def _guide_text() -> str:
    assert GUIDE.is_file(), f"guide missing: {GUIDE}"
    return GUIDE.read_text(encoding="utf-8")


def _manifest(text: str) -> dict[str, list[str]]:
    start = text.index(MANIFEST_START) + len(MANIFEST_START)
    end = text.index(MANIFEST_END, start)
    block = text[start:end]
    fence = re.search(r"```json\s*(\{.*?\})\s*```", block, re.DOTALL)
    assert fence, "no json code block between the MCP-TOOL-MANIFEST markers"
    manifest = json.loads(fence.group(1))
    assert isinstance(manifest, dict) and manifest, "manifest is empty"
    for server, tools in manifest.items():
        assert isinstance(tools, list) and tools, f"{server}: no tools listed"
    return manifest


def _registered() -> dict[str, object]:
    assert OPENCODE_JSON.is_file(), f"opencode.json missing: {OPENCODE_JSON}"
    return json.loads(OPENCODE_JSON.read_text(encoding="utf-8"))["mcp"]


def _spawn_env(cfg: dict[str, object]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(cfg.get("environment") or {})  # type: ignore[arg-type]
    # The check must not touch the network: the models and venvs are on disk.
    env.setdefault("UV_OFFLINE", "1")
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("TRANSFORMERS_OFFLINE", "1")
    return env


async def _list_live_tools(cfg: dict[str, object], timeout: float = 180.0) -> list[str]:
    argv = cfg["command"]  # type: ignore[index]
    assert isinstance(argv, list) and argv, "registered command is not a list"
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=_spawn_env(cfg))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout)
            listing = await asyncio.wait_for(session.list_tools(), timeout=timeout)
    return sorted(tool.name for tool in listing.tools)


async def _live_all(registered: dict[str, object]) -> dict[str, list[str]]:
    live: dict[str, list[str]] = {}
    for server in registered:
        live[server] = await _list_live_tools(registered[server])  # type: ignore[arg-type]
    return live


def _keychain_token(service: str, account: str) -> str | None:
    try:
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    token = proc.stdout.strip()
    return token or None


def test_manifest_matches_running_setup() -> None:
    text = _guide_text()
    manifest = _manifest(text)
    registered = _registered()

    # 1. Guide servers == registered servers.
    assert set(manifest) == set(registered), (
        "guide servers differ from opencode.json mcp keys: "
        f"guide={sorted(manifest)} registered={sorted(registered)}"
    )

    # 4. Every manifest tool name appears in the guide text (not only the block).
    for server, tools in manifest.items():
        for name in tools:
            assert name in text, f"{server}/{name} is in the manifest but not the guide"

    if os.environ.get("VS_GUIDE_SKIP_LIVE") == "1":
        pytest.skip("VS_GUIDE_SKIP_LIVE=1: manifest/config checked, live skipped")

    live = asyncio.run(_live_all(registered))

    # 2. Manifest == live tools/list, and 3. live names appear in the guide.
    for server, names in manifest.items():
        assert server in live, f"server '{server}' produced no live tools/list"
        documented = set(names)
        running = set(live[server])
        missing = sorted(documented - running)
        undocumented = sorted(running - documented)
        assert not missing, (
            f"{server}: guide names tools that are NOT in the running server: {missing}"
        )
        assert not undocumented, (
            f"{server}: running tools are MISSING from the guide: {undocumented}"
        )
        for name in running:
            assert name in text, f"{server}/{name} is live but absent from the guide"


def test_guide_contains_no_secrets() -> None:
    text = _guide_text()
    for label, (service, account) in KEYCHAIN_SECRETS.items():
        token = _keychain_token(service, account)
        if token:
            assert token not in text, f"guide leaks the {label} from the Keychain"
