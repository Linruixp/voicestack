#!/bin/bash
# Task 31 — benchmark the meeting pipeline against PRE-COMMITTED ceilings.
#
# Ceilings (see docs/bench.md, written BEFORE any measurement):
#   peak RSS  <= 5.0 GiB   (docs/budget.md service components 3.0+1.5+0.5)
#   wall time <= 3 * audio duration
# VoiceStudio MUST be resident (:3900) for the whole run.
#
# `bash tests/bench.sh` exits 0 ONLY when both measured values are <= the
# pre-committed ceilings. On breach it executes a documented remediation
# (release the mlx-whisper weights before diarization loads) and re-measures,
# exiting non-zero only if the re-measured values still breach.
#
# `bash tests/bench.sh --falsify` re-runs the SAME comparator against the real
# recorded measurement with deliberately-lowered ceilings (1 byte / 1 s) and
# asserts it fails — proving the gate is real, not a rubber stamp.
#
# Evidence: ~/.omo/evidence/voicestudio-omo-local-stack/task-31-bench.txt

set -uo pipefail

REPO=/Users/LinRui/voicestack
SD="$REPO/speaker-service"
PY="$SD/.venv/bin/python"
EVID="$HOME/.omo/evidence/voicestudio-omo-local-stack"
AUDIO="$REPO/fixtures/meetings/meeting_20min.wav"
DRIVER="$SD/tests/bench_driver.py"
CHECK="$SD/tests/bench_check.py"
TXT="$EVID/task-31-bench.txt"
MEAS="$EVID/task-31-measurement.json"

# ── PRE-COMMITTED CEILINGS (docs/bench.md; NOT derived from any measurement) ──
RSS_CEILING="${BENCH_RSS_CEILING_BYTES:-5368709120}"   # 5.0 GiB
WALL_FACTOR="${BENCH_WALL_FACTOR:-3}"                  # wall <= 3x audio duration

mkdir -p "$EVID"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$TXT"; }

audio_duration() {
  ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$AUDIO"
}

vs_rss_kb() {
  local total=0 pid rss
  for pid in $(pgrep -f 'VoiceStudio.app' 2>/dev/null); do
    rss=$(ps -o rss= -p "$pid" 2>/dev/null)
    total=$((total + ${rss:-0}))
  done
  echo "$total"
}

therm_line() { pmset -g therm 2>&1 | tr '\n' '|'; }

build_measurement() {
  # $1 driver-json $2 time-log $3 disk-kb $4 remediation $5 vs-rss-kb $6 out
  local djson="$1" tlog="$2" dkb="$3" remed="$4" vsrss="$5" out="$6"
  local real maxrss foot
  real=$(awk '$2=="real"{print $1; exit}' "$tlog")
  maxrss=$(awk '$2=="maximum"&&$3=="resident"{print $1; exit}' "$tlog")
  foot=$(awk '$2=="peak"&&$3=="memory"{print $1; exit}' "$tlog")
  "$PY" - "$djson" "$out" "${real:-0}" "${maxrss:-0}" "${foot:-0}" \
        "$dkb" "$DUR" "${WALL_CEILING:-0}" "$remed" "$vsrss" <<'PY'
import json, sys
(djson, out, real, maxrss, foot, dkb, dur, wceil, remed, vsrss) = sys.argv[1:11]
d = json.load(open(djson))
m = {
    "wall_s": float(real),
    "driver_wall_s": d["wall_s"],
    "peak_rss_bytes": int(maxrss),
    "peak_footprint_bytes": int(foot),
    "disk_delta_kb": int(dkb),
    "audio_duration_s": float(dur),
    "wall_ceiling_s": float(wceil),
    "voice_studio_rss_kb": int(vsrss),
    "remediation": remed,
    "segments": d["segments"],
    "unknown_clusters": d["unknown_clusters"],
    "speakers": d["speakers"],
    "language": d["language"],
    "warnings": d["warnings"],
}
json.dump(m, open(out, "w"), indent=2)
print(json.dumps({k: m[k] for k in
                  ("wall_s", "peak_rss_bytes", "peak_footprint_bytes",
                   "disk_delta_kb", "remediation")}))
PY
}

# ── falsifiability mode (no measurement run) ──────────────────────────────────
if [ "${1:-}" = "--falsify" ]; then
  DUR=$(audio_duration)
  WALL_CEILING=$(awk -v d="$DUR" -v f="$WALL_FACTOR" 'BEGIN{printf "%.4f", d*f}')
  if [ ! -f "$MEAS" ]; then
    log "no measurement at $MEAS; run '$0' first"
    exit 2
  fi
  log "=== falsifiability check (comparator = tests/bench_check.py) ==="
  log "recorded measurement: $(cat "$MEAS" | tr -d '\n' | cut -c1-200)"
  log "-- real pre-committed ceilings (expect PASS) --"
  "$PY" "$CHECK" --measurement "$MEAS" --rss-ceiling "$RSS_CEILING" --wall-ceiling "$WALL_CEILING" | tee -a "$TXT"
  rc_real="${PIPESTATUS[0]}"
  log "-- deliberately lowered ceilings 1B / 1s (expect FAIL) --"
  "$PY" "$CHECK" --measurement "$MEAS" --rss-ceiling 1 --wall-ceiling 1 | tee -a "$TXT"
  rc_low="${PIPESTATUS[0]}"
  if [ "$rc_real" -eq 0 ] && [ "$rc_low" -ne 0 ]; then
    log "FALSIFIABILITY PROVEN: real ceilings PASS (rc=0); lowered ceilings FAIL (rc=$rc_low)"
    exit 0
  fi
  log "FALSIFIABILITY FAILED: real rc=$rc_real lowered rc=$rc_low"
  exit 1
fi

# ── real run ──────────────────────────────────────────────────────────────────
: > "$TXT"
log "=== task 31: meeting pipeline vs pre-committed ceilings ==="
log "repo HEAD: $(GIT_MASTER=1 git -C "$REPO" rev-parse --short HEAD)"
log "fixture: $AUDIO"

DUR=$(audio_duration)
WALL_CEILING="${BENCH_WALL_CEILING_S:-$(awk -v d="$DUR" -v f="$WALL_FACTOR" 'BEGIN{printf "%.4f", d*f}')}"
log "audio duration: ${DUR}s"
log "PRE-COMMITTED ceilings: peak RSS <= ${RSS_CEILING} B ; wall <= ${WALL_CEILING}s (${WALL_FACTOR}x audio)"
log "host: $(sysctl -n hw.model 2>/dev/null) $(uname -m), 24 GB fanless"

# ── require VoiceStudio resident ─────────────────────────────────────────────
if ! "$REPO/bin/vs-ensure-up.sh" >>"$TXT" 2>&1; then
  log "VoiceStudio ensure-up FAILED — refusing to benchmark without it resident"
  exit 3
fi
VSHTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  http://127.0.0.1:3900/.well-known/voicestudio-speech)
if [ "$VSHTTP" != "200" ]; then
  log "VoiceStudio not resident (HTTP ${VSHTTP}) — refusing to benchmark"
  exit 3
fi
log "VoiceStudio RESIDENT: discovery HTTP ${VSHTTP}; pids=$(pgrep -f 'VoiceStudio.app' | paste -sd, -) rss=$(vs_rss_kb) kB"
log "thermal-before: $(therm_line)"
log "load-before: $(uptime)"

# ── isolate + offline ────────────────────────────────────────────────────────
DATA="$(mktemp -d /tmp/vs31-data-XXXXXX)"
TRDIR="$HOME/Library/Application Support/VoiceStudioStack/transcripts"
export VASTACK_DATA_DIR="$DATA"
export VASTACK_IDLE_EXIT_S=7200
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 UV_OFFLINE=1
log "isolated data dir: $DATA"

run_once() {
  local tag="$1" remed="$2" release="$3"
  local djson="$EVID/task-31-driver-$tag.json"
  local dout="$EVID/task-31-driver-$tag.log"
  local tlog="$EVID/task-31-time-$tag.log"
  local before_data after_data before_tr after_tr dkb
  before_data=$(du -sk "$DATA" 2>/dev/null | awk '{print $1}'); before_data=${before_data:-0}
  before_tr=$(du -sk "$TRDIR" 2>/dev/null | awk '{print $1}'); before_tr=${before_tr:-0}
  log "--- run [$tag] remediation=${remed} release_whisper=${release} ---"
  ( cd "$SD" && BENCH_RELEASE_WHISPER="$release" BENCH_AUDIO="$AUDIO" \
      BENCH_DRIVER_OUT="$djson" \
      /usr/bin/time -l "$PY" "$DRIVER" >"$dout" 2>"$tlog" )
  local rc=$?
  after_data=$(du -sk "$DATA" 2>/dev/null | awk '{print $1}'); after_data=${after_data:-0}
  after_tr=$(du -sk "$TRDIR" 2>/dev/null | awk '{print $1}'); after_tr=${after_tr:-0}
  dkb=$(( (after_data - before_data) + (after_tr - before_tr) ))
  grep -E 'maximum resident set size|peak memory footprint|real ' "$tlog" | sed 's/^/    time -l: /' | tee -a "$TXT"
  [ -f "$djson" ] || { log "driver produced no summary (rc=$rc): $(tail -3 "$tlog")"; return 1; }
  log "driver summary: $(tr -d '\n' < "$djson")"
  build_measurement "$djson" "$tlog" "$dkb" "$remed" "$(vs_rss_kb)" "$MEAS" \
    | sed 's/^/    measured: /' | tee -a "$TXT"
  return 0
}

check() {
  "$PY" "$CHECK" --measurement "$MEAS" \
    --rss-ceiling "$RSS_CEILING" --wall-ceiling "$WALL_CEILING" | tee -a "$TXT"
  return "${PIPESTATUS[0]}"
}

run_once baseline none 0 || { log "baseline run failed"; exit 4; }
log "--- ceiling comparison (baseline) ---"
if check; then
  log "RESULT: PASS — within pre-committed ceilings (no remediation needed)"
  log "thermal-after: $(therm_line)"
  log "load-after: $(uptime)"
  exit 0
fi

log "RESULT: BREACH — executing documented remediation ladder (docs/bench.md §4)"
REMED="release mlx-whisper weights before diarization (free ~3GB resident)"
log "REMEDIATION: $REMED"
run_once remediated "$REMED" 1 || { log "remediated run failed"; exit 4; }
log "--- ceiling comparison (re-measured after remediation) ---"
if check; then
  log "RESULT: PASS after remediation — $REMED"
  log "thermal-after: $(therm_line)"
  exit 0
fi
log "RESULT: STILL BREACHING after remediation — escalate (smaller whisper model / pin CPU)"
log "thermal-after: $(therm_line)"
exit 1
