#!/usr/bin/env python3
"""Task 28 smoke test: spawn each registered MCP server over stdio, aggregate
``tools/list``, and call one tool per registered server (seven calls total).

Run with the speaker-service venv python (it declares ``mcp==2.2.0``)::

    /Users/LinRui/voicestack/speaker-service/.venv/bin/python \
        /Users/LinRui/voicestack/bin/mcp_smoke.py \
        --out ~/.omo/evidence/voicestudio-omo-local-stack/task-28-mcp.json

The server commands and environments are read straight from the registered
``~/.config/opencode/opencode.json`` ``mcp`` block, so the smoke test exercises
exactly what OpenCode will spawn. Every result is recorded verbatim (large
blobs summarised, not dropped), so a failure here is evidence, never silence.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path("/Users/LinRui/voicestack")
FIXTURES = REPO / "fixtures"
OPENCODE_JSON = Path("/Users/LinRui/.config/opencode/opencode.json")


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


# task tool -> (registered server key, arguments, timeout_seconds)
CALLS: dict[str, tuple[str, dict[str, Any], float]] = {
    "generate_speech": (
        "vsbridge",
        {
            "text": "Hello from the VoiceStudio on-demand MCP smoke test.",
            "language": "en",
        },
        180.0,
    ),
    "clone_voice": (
        "vsbridge",
        {
            "name": "smoke-clone",
            # VoiceStudio refuses input paths unless OMNIVOICE_MCP_BASE_PATH is set,
            # so feed the reference clip inline (base64) as the live tool expects.
            "ref_audio_base64": _b64(FIXTURES / "refs" / "ref_en.wav"),
        },
        180.0,
    ),
    "generate_audiobook": (
        "voicebridge",
        {
            "import_path": str(
                REPO / "voicebridge" / "tests" / "fixtures" / "tiny.txt"
            ),
            "language": "en",
            "format": "m4b",
        },
        420.0,
    ),
    "translate_text": (
        "voicebridge",
        {
            "text": "Hello, this is a smoke test of the translation bridge.",
            "src": "en",
            "tgt": "zh",
        },
        240.0,
    ),
    "transcribe_meeting": (
        "vs-speaker",
        {"file_path": str(FIXTURES / "meetings" / "spkA_rec1.wav")},
        420.0,
    ),
    "identify_speaker": (
        "vs-speaker",
        {"audio_path": str(FIXTURES / "meetings" / "spkA_rec1.wav")},
        240.0,
    ),
    "mineru_health_check": ("mineru", {}, 90.0),
}

# registered server key -> the call names this smoke test makes on it
SERVER_CALLS: dict[str, list[str]] = {}
for _tool, (_server, _args, _timeout) in CALLS.items():
    SERVER_CALLS.setdefault(_server, []).append(_tool)


def _load_registered() -> dict[str, Any]:
    return json.loads(OPENCODE_JSON.read_text())["mcp"]


def _server_env(cfg: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(cfg.get("environment") or {})
    return env


def _summarise(value: Any) -> Any:
    """Keep structure, replace unbounded payloads (base64) with a length+digest."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and ("base64" in key.lower() or len(item) > 2000):
                out[key] = {
                    "_omitted": "blob",
                    "len": len(item),
                    "sha256_16": hashlib.sha256(item.encode("utf-8")).hexdigest()[:16],
                }
            else:
                out[key] = _summarise(item)
        return out
    if isinstance(value, list):
        return [_summarise(item) for item in value[:25]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:2000] + f"...<truncated total={len(value)}>"
    return value


def _result_record(result: Any, wall_ms: float) -> dict[str, Any]:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    text = "\n".join(parts)
    parsed: Any = None
    try:
        parsed = _summarise(json.loads(text))
    except json.JSONDecodeError:
        parsed = None
    is_error = bool(
        getattr(result, "isError", False) or getattr(result, "is_error", False)
    )
    return {
        "is_error": is_error,
        "app_error": _app_error(parsed, text),
        "content_items": len(getattr(result, "content", []) or []),
        "text_len": len(text),
        "text_head": text[:800],
        "parsed": parsed,
        "wall_ms": round(wall_ms, 1),
    }


def _app_error(parsed: Any, text: str) -> str | None:
    """Some servers return application failures as ordinary (non-error) content."""
    if isinstance(parsed, dict):
        if parsed.get("ok") is False:
            return "ok=false"
        if "error" in parsed:
            error = parsed["error"]
            return error if isinstance(error, str) else "error field present"
    if "\u274c" in text:
        return "status report contains a failure marker"
    return None


async def _list_tools(
    argv: list[str], env: dict[str, str], timeout: float
) -> dict[str, Any]:
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)
    started = time.monotonic()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), timeout=timeout)
            listing = await asyncio.wait_for(session.list_tools(), timeout=timeout)
    tools = [
        {
            "name": tool.name,
            "description": (tool.description or "")[:300],
            "input_schema_props": sorted(
                (tool.input_schema or {}).get("properties", {}).keys()
            ),
        }
        for tool in listing.tools
    ]
    return {
        "server_info": {
            "name": getattr(init.server_info, "name", None),
            "version": getattr(init.server_info, "version", None),
        },
        "tool_count": len(tools),
        "tools": tools,
        "list_wall_ms": round((time.monotonic() - started) * 1000, 1),
    }


async def _call_tool(
    argv: list[str],
    env: dict[str, str],
    call_name: str,
    args: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=60.0)
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    session.call_tool(call_name, arguments=args), timeout=timeout
                )
            except asyncio.TimeoutError:
                return {
                    "is_error": True,
                    "error": f"client timeout after {timeout:.0f}s",
                    "wall_ms": round((time.monotonic() - started) * 1000, 1),
                }
    record = _result_record(result, (time.monotonic() - started) * 1000)
    record["arguments"] = args
    return record


async def _run(selected: list[str]) -> dict[str, Any]:
    registered = _load_registered()
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task": 28,
        "purpose": "aggregated tools/list + one tools/call per registered MCP server",
        "registered_mcp_config": registered,
        "servers": {},
    }
    aggregated: dict[str, list[str]] = {}
    for server, call_names in SERVER_CALLS.items():
        if selected and server not in selected:
            continue
        cfg = registered.get(server)
        entry: dict[str, Any] = {
            "command": (cfg or {}).get("command"),
            "environment": (cfg or {}).get("environment"),
            "calls": {},
        }
        if not cfg:
            entry["list_error"] = (
                f"server '{server}' is NOT registered in opencode.json"
            )
            report["servers"][server] = entry
            aggregated[server] = []
            continue
        argv = cfg["command"]
        env = _server_env(cfg)
        try:
            entry.update(await _list_tools(argv, env, 90.0))
            aggregated[server] = [tool["name"] for tool in entry["tools"]]
        except Exception as exc:  # noqa: BLE001 - record, never abort the sweep
            entry["list_error"] = f"{type(exc).__name__}: {exc}"
            aggregated[server] = []
        for call_name in call_names:
            _, args, timeout = CALLS[call_name]
            try:
                entry["calls"][call_name] = await _call_tool(
                    argv, env, call_name, args, timeout
                )
            except Exception as exc:  # noqa: BLE001
                entry["calls"][call_name] = {
                    "is_error": True,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        report["servers"][server] = entry
    report["aggregated_tools"] = aggregated
    report["summary"] = {
        "servers_listed": sum(1 for v in report["servers"].values() if v.get("tools")),
        "servers_list_error": [
            k for k, v in report["servers"].items() if v.get("list_error")
        ],
        "calls_ok": sorted(
            f"{s}.{c}"
            for s, v in report["servers"].items()
            for c, r in v.get("calls", {}).items()
            if not r.get("is_error") and not r.get("app_error")
        ),
        "calls_failed": sorted(
            f"{s}.{c}"
            for s, v in report["servers"].items()
            for c, r in v.get("calls", {}).items()
            if r.get("is_error") or r.get("app_error")
        ),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--server", action="append", default=[], help="limit to server(s)"
    )
    parsed = parser.parse_args()
    report = asyncio.run(_run(parsed.server))
    parsed.out.parent.mkdir(parents=True, exist_ok=True)
    parsed.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report["summary"], indent=2))
    print(f"wrote {parsed.out}")


if __name__ == "__main__":
    sys.exit(main())
