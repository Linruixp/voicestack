"""Acceptance: EN/ZH text-PDF audiobooks + scanned-PDF-via-MinerU audiobook.

Also owns the MinerU health capability. Asserts on the produced ``.m4b`` artifact
with an independent ``ffprobe``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acceptance_support import (
    FIXTURES,
    VOICE_EN,
    VOICE_ZH,
    Reporter,
    Session,
    ffprobe,
)


async def render_audiobook(
    s: Session,
    rep: Reporter,
    evidence: Path,
    key: str,
    src: Path,
    voice: str,
    language: str,
) -> bool:
    rec = await s.call(
        "generate_audiobook",
        {
            "import_path": str(src),
            "voice": voice,
            "language": language,
            "format": "m4b",
        },
    )
    parsed = rec.get("parsed") or {}
    ok = s.ok(rec) and parsed.get("ok") is True
    out = Path(parsed["output_path"]) if ok and parsed.get("output_path") else None
    dest = evidence / f"audiobook_{key}.m4b"
    probe: dict[str, Any] = {}
    if out is not None and out.is_file():
        dest.write_bytes(out.read_bytes())
        probe = ffprobe(dest)
    return rep.check(
        f"audiobook_{key}",
        str(dest),
        "m4b exists, aac codec, >=1 chapter, duration>0",
        ok
        and dest.is_file()
        and probe.get("audio_codec") == "aac"
        and int(probe.get("chapter_count") or 0) >= 1
        and float(probe.get("duration_s") or 0) > 0,
        {
            "src": str(src),
            "tool_wall_ms": rec["wall_ms"],
            "tool_output": parsed.get("filename"),
            "ffprobe": probe,
        },
    )


async def audiobooks(sess: dict[str, Session], rep: Reporter, ev: Path) -> None:
    en = await render_audiobook(
        sess["voicebridge"],
        rep,
        ev,
        "en_text_pdf",
        FIXTURES / "docs" / "en_text.pdf",
        VOICE_EN,
        "en",
    )
    zh = await render_audiobook(
        sess["voicebridge"],
        rep,
        ev,
        "zh_text_pdf",
        FIXTURES / "docs" / "zh_text.pdf",
        VOICE_ZH,
        "zh",
    )
    scanned = await sess["mineru"].call(
        "mineru_parse_document",
        {
            "file_path": str(FIXTURES / "docs" / "scanned.pdf"),
            "output_format": "markdown",
        },
    )
    md_text = scanned["text"] or ""
    md_path = ev / "scanned.mineru.md"
    md_path.write_text(md_text, encoding="utf-8")
    rep.check(
        "mineru_scanned_markdown",
        str(md_path),
        "mineru OCR returned markdown (>500 chars, has a heading)",
        not scanned["is_error"] and len(md_text) > 500 and "#" in md_text,
        {"tool_wall_ms": scanned["wall_ms"], "chars": len(md_text)},
    )
    scan = await render_audiobook(
        sess["voicebridge"], rep, ev, "scanned_mineru", md_path, VOICE_EN, "en"
    )
    rep.cap(
        "generate_audiobook",
        "voicebridge",
        "generate_audiobook",
        bool(en and zh and scan),
        {
            "en": "audiobook_en_text_pdf.m4b",
            "zh": "audiobook_zh_text_pdf.m4b",
            "scanned": "audiobook_scanned_mineru.m4b",
        },
    )


async def mineru_health_capability(sess: dict[str, Session], rep: Reporter) -> None:
    health = await sess["mineru"].call("mineru_health_check", {})
    htext = health["text"] or ""
    rep.cap(
        "mineru_health_check",
        "mineru",
        "mineru_health_check",
        (not health["is_error"]) and "MinerU CLI" in htext and "\u274c" not in htext,
        {"head": htext.splitlines()[:1]},
    )
