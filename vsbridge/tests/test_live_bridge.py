"""Live acceptance test — skipped unless ``VS_BRIDGE_LIVE=1``.

Quits VoiceStudio, then drives the real stdio MCP server so ``tools/call
generate_speech`` cold-starts VoiceStudio via ``vs-ensure-up.sh`` and returns
audio. A second, warm call must be fast and must NOT relaunch the app (the
backend PID is unchanged). Writes an evidence JSON when ``VS_BRIDGE_EVIDENCE``
is set (defaults to the task-37 evidence path).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import bridge_session

pytestmark = pytest.mark.skipif(
    os.environ.get("VS_BRIDGE_LIVE") != "1",
    reason="live VoiceStudio cold-start test (set VS_BRIDGE_LIVE=1)",
)

EVIDENCE_PATH = Path(
    os.environ.get(
        "VS_BRIDGE_EVIDENCE",
        os.path.expanduser(
            "~/.omo/evidence/voicestudio-omo-local-stack/task-37-bridge.json"
        ),
    )
)

LIVE_TOOLS_UP = {
    "generate_speech",
    "list_voices",
    "list_personalities",
    "list_languages",
    "transcribe",
    "check_health",
    "clone_voice",
}
# Saved VoiceStudio profile from task 8.
VOICE_EN = "56ee2a5b"
TEXT_EN = "The lighthouse keeper watched the storm roll in."


def _port_up(port: int = 3900) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _backend_pid() -> str | None:
    proc = subprocess.run(
        ["pgrep", "-f", "uvicorn main:app"],
        capture_output=True,
        text=True,
    )
    pids = [p for p in proc.stdout.split() if p.strip()]
    return pids[0] if pids else None


def _app_running() -> bool:
    return (
        subprocess.run(["pgrep", "-x", "VoiceStudio"], capture_output=True).returncode
        == 0
    )


def _quit_voicestudio(timeout_s: float = 60.0) -> bool:
    """Fully terminate VoiceStudio.

    ``quit app`` alone can leave the Electron app alive with only its backend
    down; a lingering app makes ``open -a`` a no-op, so the cold start would
    stall. Wait for the port to drop, then force-kill any remaining app process
    and confirm it is gone before returning.
    """
    subprocess.run(
        ["osascript", "-e", 'quit app "VoiceStudio"'], capture_output=True, text=True
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and (_port_up() or _app_running()):
        time.sleep(1.0)
    for _ in range(5):
        if not _app_running() and not _port_up():
            break
        subprocess.run(
            ["pkill", "-f", "/Applications/VoiceStudio.app"], capture_output=True
        )
        subprocess.run(["pkill", "-x", "VoiceStudio"], capture_output=True)
        time.sleep(2.0)
    return not _port_up() and not _app_running()


def _audio_payload(result: Any) -> dict[str, Any]:
    """Parse the payload VoiceStudio returns inside content[0].text.

    generate_speech returns a JSON string; check_health returns a Python repr
    (``{'status': 'ok', ...}``) — the raw form is kept for non-JSON tools.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and "result" in structured:
        raw = structured["result"]
        if isinstance(raw, dict):
            return raw
        text = raw if isinstance(raw, str) else json.dumps(raw)
    else:
        text = result.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def test_live_cold_start_generate_speech_then_warm_no_relaunch() -> None:
    report: dict = {
        "task": 37,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bridge": "vsbridge",
        "transport": "stdio -> mcp-streamable-http",
        "mcp_endpoint": "http://127.0.0.1:3900/mcp/",
        "ensure_up_script": "~/voicestack/bin/vs-ensure-up.sh",
    }

    # ── cold-start precondition: VoiceStudio must be DOWN ────────────────────
    report["port_up_before"] = _port_up()
    stopped = _quit_voicestudio()
    report["stopped_voicestudio"] = stopped
    report["port_up_after_quit"] = _port_up()
    assert stopped and not _port_up(), (
        "could not bring VoiceStudio down for a cold start"
    )

    async def drive() -> dict:
        out: dict = {}
        async with bridge_session(
            {"VS_MCP_TIMEOUT": "300", "VS_ENSURE_TIMEOUT": "180"}
        ) as session:
            listed = await session.list_tools()
            out["tools_list_names"] = sorted(t.name for t in listed.tools)

            # ── cold call: generate_speech must start VoiceStudio ────────────
            cold_started = time.monotonic()
            cold = await session.call_tool(
                "generate_speech",
                {"text": TEXT_EN, "language": "en", "profile_id": VOICE_EN},
            )
            out["cold_wall_s"] = round(time.monotonic() - cold_started, 2)
            out["cold_is_error"] = bool(getattr(cold, "isError", False))
            out["cold_audio"] = _audio_payload(cold)
            out["port_up_during_cold"] = _port_up()
            out["cold_backend_pid"] = _backend_pid()

            # ── warm call: app already up → fast, no relaunch ────────────────
            warm_started = time.monotonic()
            warm = await session.call_tool("check_health", {})
            out["warm_wall_s"] = round(time.monotonic() - warm_started, 2)
            out["warm_is_error"] = bool(getattr(warm, "isError", False))
            out["warm_payload"] = _audio_payload(warm)
            out["warm_backend_pid"] = _backend_pid()
        return out

    result = asyncio.run(drive())

    # ── assertions: cold start ───────────────────────────────────────────────
    assert result["port_up_during_cold"], "VoiceStudio was not up after the cold call"
    assert not result["cold_is_error"], (
        f"cold generate_speech failed: {result['cold_audio']}"
    )
    audio = result["cold_audio"]
    assert audio.get("format") == "wav", f"unexpected audio format: {audio}"
    b64 = audio.get("wav_base64") or ""
    assert b64, "generate_speech returned no wav_base64"
    wav = base64.b64decode(b64)
    assert wav[:4] == b"RIFF", "decoded audio is not a RIFF/WAV container"

    report["cold"] = {
        "tool": "generate_speech",
        "wall_s": result["cold_wall_s"],
        "ok": True,
        "audio_id": audio.get("audio_id"),
        "format": audio.get("format"),
        "audio_duration_s": audio.get("audio_duration_s"),
        "generation_time_s": audio.get("generation_time_s"),
        "wav_bytes": len(wav),
        "port_up_during": result["port_up_during_cold"],
        "backend_pid": result["cold_backend_pid"],
    }

    # ── assertions: warm call does not relaunch ──────────────────────────────
    assert not result["warm_is_error"], (
        f"warm check_health failed: {result['warm_payload']}"
    )
    assert result["cold_backend_pid"], "could not read backend pid"
    assert result["warm_backend_pid"] == result["cold_backend_pid"], (
        "warm call relaunched VoiceStudio (backend pid changed)"
    )
    assert result["warm_wall_s"] < 15, (
        f"warm call took {result['warm_wall_s']}s — likely a relaunch"
    )

    report["warm"] = {
        "tool": "check_health",
        "wall_s": result["warm_wall_s"],
        "ok": True,
        "relaunch": False,
        "backend_pid": result["warm_backend_pid"],
        "pid_unchanged": True,
    }
    report["tools_list_count"] = len(result["tools_list_names"])
    report["tools_list_names"] = result["tools_list_names"]
    assert set(result["tools_list_names"]) == LIVE_TOOLS_UP, (
        f"bridge did not mirror the live tool surface: {result['tools_list_names']}"
    )

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n[evidence] wrote {EVIDENCE_PATH}")
    print(json.dumps(report, indent=2, ensure_ascii=False))
