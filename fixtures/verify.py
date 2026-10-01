#!/usr/bin/env python3
"""Verify every fixture: existence, format, duration, content and internal
consistency between the audio and its ground-truth JSON.

Exits non-zero if any assertion fails. Prints an ffprobe table and a
PASS/FAIL summary suitable for the task evidence file.
"""

from __future__ import annotations

import json
import subprocess
import sys
import wave
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import AUDIO, DOCS, MEETINGS, REFS, ROOT, SR  # noqa: E402

failures: list[str] = []
rows: list[tuple] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def ffprobe(path: Path) -> dict:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(out)


def audio_row(path: Path, label: str) -> float:
    info = ffprobe(path)
    st = info["streams"][0]
    dur = float(info["format"]["duration"])
    rows.append(
        (
            label,
            st.get("codec_name"),
            st.get("sample_rate"),
            st.get("channels"),
            f"{dur:.3f}",
        )
    )
    check(st.get("codec_name") == "pcm_s16le", f"{label}: codec != pcm_s16le")
    check(int(st.get("sample_rate", 0)) == SR, f"{label}: rate != 16k")
    check(int(st.get("channels", 0)) == 1, f"{label}: not mono")
    return dur


def main() -> int:
    # --- expected files ---------------------------------------------------
    expected = [
        AUDIO / "en_30s.wav",
        AUDIO / "zh_30s.wav",
        AUDIO / "clip_3s.wav",
        AUDIO / "silence_5s.wav",
        ROOT / "not_audio.txt",
        DOCS / "en_text.pdf",
        DOCS / "zh_text.pdf",
        DOCS / "book.epub",
        DOCS / "scanned.pdf",
        MEETINGS / "spkA_rec1.wav",
        MEETINGS / "spkA_rec2.wav",
        MEETINGS / "spkB_rec1.wav",
        MEETINGS / "spkB_rec2.wav",
        MEETINGS / "ground_truth.json",
        MEETINGS / "meeting_20min.wav",
        MEETINGS / "meeting_20min.ground_truth.json",
        REFS / "ref_en.wav",
        REFS / "ref_zh.wav",
    ]
    for p in expected:
        check(p.exists() and p.stat().st_size > 0, f"missing/empty: {p}")

    # --- audio durations --------------------------------------------------
    d = audio_row(AUDIO / "en_30s.wav", "audio/en_30s.wav")
    check(27 <= d <= 34, f"en_30s.wav duration {d:.2f} not ~30s")
    d = audio_row(AUDIO / "zh_30s.wav", "audio/zh_30s.wav")
    check(27 <= d <= 34, f"zh_30s.wav duration {d:.2f} not ~30s")
    d = audio_row(AUDIO / "clip_3s.wav", "audio/clip_3s.wav")
    check(2.9 <= d <= 3.1, f"clip_3s.wav duration {d:.2f} not 3s")
    d = audio_row(AUDIO / "silence_5s.wav", "audio/silence_5s.wav")
    check(4.9 <= d <= 5.1, f"silence_5s.wav duration {d:.2f} not 5s")
    # silence must actually be silent
    with wave.open(str(AUDIO / "silence_5s.wav"), "rb") as w:
        check(
            all(b == 0 for b in w.readframes(w.getnframes())),
            "silence_5s.wav contains non-zero samples",
        )
    for name, lo, hi in [("ref_en.wav", 10, 20), ("ref_zh.wav", 10, 20)]:
        d = audio_row(REFS / name, f"refs/{name}")
        check(lo <= d <= hi, f"{name} duration {d:.2f} not {lo}-{hi}s")

    for name in ["spkA_rec1.wav", "spkA_rec2.wav", "spkB_rec1.wav", "spkB_rec2.wav"]:
        d = audio_row(MEETINGS / name, f"meetings/{name}")
        check(d >= 10, f"{name} too short ({d:.2f}s)")
    d = audio_row(MEETINGS / "meeting_20min.wav", "meetings/meeting_20min.wav")
    check(1140 <= d <= 1260, f"meeting_20min duration {d:.2f} not ~20min")

    # --- non-audio file ---------------------------------------------------
    rc = subprocess.run(
        ["ffprobe", "-v", "error", str(ROOT / "not_audio.txt")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    check(rc != 0, "not_audio.txt was unexpectedly probed as valid audio")

    # --- PDF text ---------------------------------------------------------
    def pdf_text(p: Path) -> str:
        return subprocess.check_output(["pdftotext", str(p), "-"]).decode(
            "utf-8", "ignore"
        )

    en = pdf_text(DOCS / "en_text.pdf")
    check(len(en.strip()) > 300, "en_text.pdf has too little selectable text")
    check("Coastal Weather Station" in en, "en_text.pdf heading missing")
    zh = pdf_text(DOCS / "zh_text.pdf")
    check(len(zh.strip()) > 80, "zh_text.pdf has too little selectable text")
    check("气象站" in zh, "zh_text.pdf heading missing")
    sc = pdf_text(DOCS / "scanned.pdf")
    check(
        len(sc.strip()) == 0,
        f"scanned.pdf must have NO text layer (found {len(sc.strip())} chars)",
    )

    # --- EPUB structure ---------------------------------------------------
    with zipfile.ZipFile(DOCS / "book.epub") as z:
        names = z.namelist()
        check(names[0] == "mimetype", "EPUB mimetype is not the first entry")
        check(
            z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED,
            "EPUB mimetype must be stored uncompressed",
        )
        check(
            z.read("mimetype") == b"application/epub+zip", "EPUB mimetype content wrong"
        )
        check("META-INF/container.xml" in names, "EPUB container.xml missing")
        check("OEBPS/content.opf" in names, "EPUB content.opf missing")
        chapters = [
            n
            for n in names
            if n.startswith("OEBPS/") and n.endswith(".xhtml") and "nav" not in n
        ]
        check(len(chapters) >= 2, f"EPUB has <2 chapters ({len(chapters)})")

    # --- ground truth consistency ----------------------------------------
    gt = json.loads((MEETINGS / "ground_truth.json").read_text())
    check(
        set(gt["files"])
        == {"spkA_rec1.wav", "spkA_rec2.wav", "spkB_rec1.wav", "spkB_rec2.wav"},
        "ground_truth.json file set mismatch",
    )
    for fname, blk in gt["files"].items():
        dur = ffprobe(MEETINGS / fname)["format"]["duration"]
        dur = float(dur)
        check(
            abs(dur - blk["duration"]) < 0.2,
            f"{fname}: gt duration {blk['duration']} vs audio {dur:.3f}",
        )
        check(len(blk["segments"]) > 0, f"{fname}: no segments")
        for s in blk["segments"]:
            check(s["start"] < s["end"], f"{fname}: bad segment {s}")
            check(s["end"] <= dur + 0.05, f"{fname}: segment past EOF")
            check(s["speaker"] == blk["speaker"], f"{fname}: speaker mismatch")
        rows.append(
            (
                f"meetings/{fname}",
                "pcm_s16le",
                SR,
                1,
                f"{dur:.3f}",
                f"{len(blk['segments'])} seg",
            )
        )

    mgt = json.loads((MEETINGS / "meeting_20min.ground_truth.json").read_text())
    mdur = float(ffprobe(MEETINGS / "meeting_20min.wav")["format"]["duration"])
    check(
        abs(mdur - mgt["duration"]) < 0.3, "meeting_20min gt duration != audio duration"
    )
    check(
        len(mgt["segments"]) == mgt["turn_count"], "meeting_20min turn_count mismatch"
    )
    check(
        len({s["speaker"] for s in mgt["segments"]}) >= 2,
        "meeting_20min has <2 speakers",
    )
    for s in mgt["segments"]:
        check(s["start"] < s["end"] <= mdur + 0.1, "meeting_20min bad segment")

    # --- report -----------------------------------------------------------
    print("FIXTURE INVENTORY (ffprobe)")
    print(f"{'file':34} {'codec':10} {'rate':>6} {'ch':>3} {'dur(s)':>9}  extra")
    for r in rows:
        extra = r[5] if len(r) > 5 else ""
        print(
            f"{r[0]:34} {str(r[1]):10} {str(r[2]):>6} {str(r[3]):>3} {r[4]:>9}  {extra}"
        )

    print()
    print(f"checks: {len(failures)} failure(s)")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("RESULT: FAIL")
        return 1
    print(
        "RESULT: PASS — all fixtures present, correctly formatted and "
        "consistent with ground truth"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
