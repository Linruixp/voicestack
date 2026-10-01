"""Live acceptance test — skipped unless ``VS_BRIDGE_LIVE=1``.

Quits VoiceStudio, then drives the real stdio MCP server so ``tools/call``
cold-starts VoiceStudio via ``vs-ensure-up.sh`` and returns a valid .m4b, then
translates text offline. Writes an evidence JSON when ``VS_BRIDGE_EVIDENCE`` is
set (defaults to the task-13 evidence path).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import bridge_session, tool_payload

pytestmark = pytest.mark.skipif(
    os.environ.get("VS_BRIDGE_LIVE") != "1",
    reason="live VoiceStudio cold-start test (set VS_BRIDGE_LIVE=1)",
)

HERE = Path(__file__).resolve().parent
TINY_DOC = HERE / "fixtures" / "tiny.txt"
EVIDENCE_PATH = Path(
    os.environ.get(
        "VS_BRIDGE_EVIDENCE",
        os.path.expanduser(
            "~/.omo/evidence/voicestudio-omo-local-stack/task-13-bridge.json"
        ),
    )
)

# Saved VoiceStudio profiles from task 8.
VOICE_EN = "base_en"
LANG_EN = "en"


def _port_up(port: int = 3900) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _quit_voicestudio(timeout_s: float = 45.0) -> bool:
    subprocess.run(
        ["osascript", "-e", 'quit app "VoiceStudio"'], capture_output=True, text=True
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _port_up():
            return True
        time.sleep(1.0)
    subprocess.run(
        ["pkill", "-f", "/Applications/VoiceStudio.app"], capture_output=True
    )
    subprocess.run(["pkill", "-x", "VoiceStudio"], capture_output=True)
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if not _port_up():
            return True
        time.sleep(1.0)
    return not _port_up()


def _cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def test_live_cold_start_audiobook_and_translation() -> None:
    report: dict = {
        "task": 13,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "doc": str(TINY_DOC),
    }

    assert TINY_DOC.is_file(), f"missing fixture {TINY_DOC}"

    # ── cold-start precondition: VoiceStudio must be DOWN ────────────────────
    was_up = _port_up()
    report["port_up_before"] = was_up
    stopped = _quit_voicestudio()
    report["stopped_voicestudio"] = stopped
    report["port_up_after_quit"] = _port_up()
    assert stopped and not _port_up(), (
        "could not bring VoiceStudio down for a cold start"
    )

    async def drive():
        out: dict = {}
        async with bridge_session({"VS_RENDER_TIMEOUT": "1800"}) as session:
            t0 = time.monotonic()
            audio = await session.call_tool(
                "generate_audiobook",
                {
                    "import_path": str(TINY_DOC),
                    "voice": VOICE_EN,
                    "language": LANG_EN,
                    "format": "m4b",
                },
            )
            out["audiobook"] = tool_payload(audio)
            out["audiobook_wall_s"] = round(time.monotonic() - t0, 2)
            out["port_up_during"] = _port_up()

            t1 = time.monotonic()
            translation = await session.call_tool(
                "translate_text",
                {
                    "text": "The lighthouse keeper watched the storm roll in.",
                    "src": "en",
                    "tgt": "zh",
                },
            )
            out["translation"] = tool_payload(translation)
            out["translation_wall_s"] = round(time.monotonic() - t1, 2)
        return out

    result = asyncio.run(drive())
    report.update(result)

    audio = result["audiobook"]
    assert audio["ok"] is True, f"audiobook failed: {audio}"
    out_path = Path(audio["output_path"])
    assert out_path.is_file(), f"missing output {out_path}"
    assert audio["size_bytes"] > 0
    probe = audio.get("ffprobe") or {}
    report["output_exists"] = out_path.is_file()
    report["output_size_bytes"] = out_path.stat().st_size
    assert probe.get("audio_codec"), f"ffprobe found no audio stream: {probe}"
    assert (probe.get("duration_s") or 0) > 0, f"zero-duration m4b: {probe}"
    assert result["port_up_during"], "VoiceStudio was not up after the cold start"

    translation = result["translation"]
    assert translation["ok"] is True, f"translation failed: {translation}"
    assert translation["translated"].strip(), "empty translation"
    report["translated_text"] = translation["translated"]
    report["translate_provider"] = translation["provider"]
    report["translate_offline"] = translation["offline"]

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n[evidence] wrote {EVIDENCE_PATH}")
    print(json.dumps(report, indent=2, ensure_ascii=False))
