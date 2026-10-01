#!/bin/bash
# generate_all.sh — Regenerate every VoiceStack test fixture from scratch.
#
# Deterministic, offline: uses macOS `say` for speech, ffmpeg/ffprobe for
# audio format + probing, reportlab/Pillow/zipfile for documents. Output is
# 16 kHz mono 16-bit PCM WAV. Audio is NOT committed (root .gitignore).
#
# Usage:
#   ./generate_all.sh
#   FIXTURES_EVIDENCE=/path/to/task-36-fixtures.txt ./generate_all.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
EVIDENCE="${FIXTURES_EVIDENCE:-}"

echo "== VoiceStack fixtures =="
date

# --- dependency gate --------------------------------------------------------
for b in say ffmpeg ffprobe python3 pdftoppm pdftotext; do
  command -v "$b" >/dev/null 2>&1 || { echo "ERROR: missing '$b'"; exit 1; }
done
python3 -c "import reportlab, PIL" 2>/dev/null \
  || { echo "ERROR: python3 needs reportlab + Pillow"; exit 1; }

mkdir -p "$DIR/audio" "$DIR/docs" "$DIR/meetings" "$DIR/refs"

# --- build ------------------------------------------------------------------
echo "-- audio (say + ffmpeg)"
python3 "$DIR/build_audio.py"
echo "-- meetings (say + labeled ground truth)"
python3 "$DIR/build_meetings.py"
echo "-- docs (text PDFs, EPUB, scanned PDF, non-audio)"
python3 "$DIR/build_docs.py"

# --- verify -----------------------------------------------------------------
echo "-- verify"
REPORT="$(mktemp)"
trap 'rm -f "$REPORT"' EXIT
python3 "$DIR/verify.py" | tee "$REPORT"

# --- evidence (optional) ----------------------------------------------------
if [ -n "$EVIDENCE" ]; then
  mkdir -p "$(dirname "$EVIDENCE")"
  {
    echo "== Task 36 fixture evidence =="
    date
    echo
    echo "-- manifest (fixture dir) --"
    ( cd "$DIR" && find . -type f ! -path './early/*' \
        ! -path '*/__pycache__/*' ! -name '*.aiff' | sort )
    echo
    echo "-- ffprobe table + assertions --"
    cat "$REPORT"
    echo
    echo "-- ground-truth sample: meetings/ground_truth.json --"
    python3 - "$DIR" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
gt = json.loads((d / "meetings/ground_truth.json").read_text())
summary = {k: {kk: vv for kk, vv in v.items() if kk != "segments"}
              | {"n_segments": len(v["segments"])}
           for k, v in gt["files"].items()}
print(json.dumps(summary, ensure_ascii=False, indent=2))
print()
print("spkA_rec1.wav segments[:2]:")
print(json.dumps(gt["files"]["spkA_rec1.wav"]["segments"][:2],
                 ensure_ascii=False, indent=2))
m = json.loads((d / "meetings/meeting_20min.ground_truth.json").read_text())
print()
print("meeting_20min.ground_truth.json (head):")
print(json.dumps({"duration": m["duration"], "turn_count": m["turn_count"],
                  "speakers": list(m["speakers"]),
                  "first_segments": m["segments"][:2]},
                 ensure_ascii=False, indent=2))
PY
  } > "$EVIDENCE"
  echo "evidence -> $EVIDENCE"
fi

echo "done."
