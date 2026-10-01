"""Shared harness for the task-30 acceptance driver.

Mechanism only: constants, an independent ``ffprobe``, a stdio ``Session`` that
spawns one registered MCP server per call, and a ``Reporter`` that records
artifact assertions and capability results. The scenario itself lives in
``acceptance_mcp.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path("/Users/LinRui/voicestack")
FIXTURES = REPO / "fixtures"
OPENCODE_JSON = Path("/Users/LinRui/.config/opencode/opencode.json")
VOICE_EN = "56ee2a5b"  # base_en profile id
VOICE_ZH = "513da105"  # base_zh profile id
VS_BASE = "http://127.0.0.1:3900"

# (server, tool) -> stdio client timeout; the server owns its own render timeouts.
TIMEOUTS: dict[tuple[str, str], float] = {
    ("voicebridge", "generate_audiobook"): 1800.0,
    ("voicebridge", "translate_text"): 300.0,
    ("vs-speaker", "transcribe_meeting"): 900.0,
    ("vs-speaker", "identify_speaker"): 300.0,
    ("vs-speaker", "enroll_speaker"): 300.0,
    ("mineru", "mineru_parse_document"): 420.0,
    ("mineru", "mineru_health_check"): 120.0,
}


def ffprobe(path: Path) -> dict[str, Any]:
    """Independent media probe - the artifact's own truth, not the tool's claim."""
    if not path.is_file():
        return {"error": "missing"}
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-print_format",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[:300]}
    raw = json.loads(proc.stdout)
    audio = next(
        (s for s in raw.get("streams", []) if s.get("codec_type") == "audio"), {}
    )
    return {
        "duration_s": round(float(raw["format"].get("duration") or 0), 3),
        "size_bytes": int(raw["format"].get("size") or 0),
        "audio_codec": audio.get("codec_name"),
        "channels": audio.get("channels"),
        "chapter_count": len(raw.get("chapters", [])),
    }


class Session:
    """One registered MCP server, spawned over stdio per call."""

    def __init__(self, key: str, cfg: dict[str, Any]) -> None:
        self.key = key
        self.command = cfg["command"]
        self.env = dict(os.environ)
        self.env.update(cfg.get("environment") or {})

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        timeout = TIMEOUTS.get((self.key, tool), 300.0)
        params = StdioServerParameters(
            command=self.command[0], args=self.command[1:], env=self.env
        )
        started = time.monotonic()
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=60.0)
                    result = await asyncio.wait_for(
                        session.call_tool(tool, arguments=args), timeout=timeout
                    )
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal to the sweep
            return {
                "is_error": True,
                "parsed": None,
                "text": f"{type(exc).__name__}: {exc}",
                "wall_ms": round((time.monotonic() - started) * 1000),
                "arguments": args,
            }
        text = "\n".join(getattr(c, "text", "") or "" for c in (result.content or []))
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        return {
            "is_error": bool(getattr(result, "isError", False)),
            "parsed": parsed,
            "text": text,
            "wall_ms": round((time.monotonic() - started) * 1000),
            "arguments": args,
        }

    @staticmethod
    def ok(rec: dict[str, Any]) -> bool:
        if rec["is_error"]:
            return False
        parsed = rec.get("parsed")
        return not (
            isinstance(parsed, dict)
            and (parsed.get("ok") is False or "error" in parsed)
        )


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def delete_profile(profile_id: str) -> None:
    req = urllib.request.Request(f"{VS_BASE}/profiles/{profile_id}", method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass


class Reporter:
    """Collects artifact assertions (``checks``) and capability results (``caps``)."""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.caps: dict[str, dict[str, Any]] = {}

    def check(
        self, name: str, artifact: str, assertion: str, ok: bool, details: Any = None
    ) -> bool:
        self.checks.append(
            {
                "name": name,
                "artifact": artifact,
                "assertion": assertion,
                "pass": bool(ok),
                "details": details or {},
            }
        )
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {assertion}", flush=True)
        return bool(ok)

    def cap(
        self, name: str, server: str, tool: str, ok: bool, evidence: Any = None
    ) -> None:
        self.caps[name] = {
            "server": server,
            "tool": tool,
            "pass": bool(ok),
            "evidence": evidence or {},
        }
        print(
            f"[{'PASS' if ok else 'FAIL'}] capability {name} ({server}.{tool})",
            flush=True,
        )

    def report(self) -> dict[str, Any]:
        failed = [c["name"] for c in self.checks if not c["pass"]]
        caps_failed = [k for k, v in self.caps.items() if not v["pass"]]
        return {
            "checks": self.checks,
            "capabilities": self.caps,
            "failed_checks": failed,
            "failed_capabilities": caps_failed,
            "overall_pass": not failed and not caps_failed,
        }
