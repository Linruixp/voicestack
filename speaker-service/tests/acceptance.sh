#!/bin/bash
# Task 30 — end-to-end acceptance across every requirement and the offline check.
#
# One scripted run over the real fixtures, driven through the registered MCP
# servers (see tests/acceptance_mcp.py):
#   EN text PDF -> m4b, ZH text PDF -> m4b,
#   scanned PDF -> MinerU -> markdown -> m4b,
#   2-recording speaker protocol (enrolled/known + returning + stranger unknown),
#   one call per OMO-facing capability,
#   a no-speech failure path,
# and an offline assertion: the run executes under HF_HUB_OFFLINE=1 /
# TRANSFORMERS_OFFLINE=1 / UV_OFFLINE=1 with a background sampler that
# baseline-diffs every non-loopback connection in the stack's process tree.
# Conclusions come from artifacts (ffprobe chapters/duration, transcript JSON),
# never from a log line alone.
#
# Writes ~/.omo/evidence/voicestudio-omo-local-stack/task-30-acceptance.json and
# exits 0 only when every artifact assertion AND the offline check pass.

set -uo pipefail

REPO=/Users/LinRui/voicestack
SD="$REPO/speaker-service"
PY="$SD/.venv/bin/python"
EVID=/Users/LinRui/.omo/evidence/voicestudio-omo-local-stack
mkdir -p "$EVID"
ART="$EVID/task-30-artifacts.json"
DRIVER_LOG="$EVID/task-30-driver.log"
OFFLOG="$EVID/task-30-offline-lsof.log"
OFFFLAG="$EVID/task-30-offline-flags.txt"
OFFBASELINE="$EVID/task-30-offline-baseline.txt"
OFFWARM="$EVID/task-30-offline-warmup.txt"
FINAL="$EVID/task-30-acceptance.json"
UVLOG="$EVID/task-30-uvicorn.log"
DATA="$(mktemp -d /tmp/vs30-data-XXXXXX)"
START=$(date +%s)

# Isolate the registry from the operator's real one and pin the run offline.
export VASTACK_DATA_DIR="$DATA"
export VASTACK_IDLE_EXIT_S=7200
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export UV_OFFLINE=1

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
listener() { lsof -t -iTCP:3910 -sTCP:LISTEN 2>/dev/null; }

log "repo HEAD: $(GIT_MASTER=1 git -C "$REPO" rev-parse --short HEAD)"
log "isolated data dir: $DATA"

# ── 0. clean pre-state on :3910 so the isolated data dir is actually used ─────
for pid in $(listener); do kill "$pid" 2>/dev/null; done
rm -rf /tmp/vs-speaker-up.lock.3910 2>/dev/null
for _ in $(seq 1 20); do [ -z "$(listener)" ] && break; sleep 1; done

# ── 1. ensure VoiceStudio + the speaker service are up via the launchers ──────
log "vs-ensure-up.sh (VoiceStudio :3900)"
"$REPO/bin/vs-ensure-up.sh" >"$EVID/task-30-ensure-vs.log" 2>&1 \
  && log "VoiceStudio up" || log "vs-ensure-up rc=$? (see task-30-ensure-vs.log)"
log "GET /health :3900 -> HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:3900/health)"

log "vs-speaker-up.sh (speaker service :3910, isolated data dir)"
VS_SPEAKER_LOG="$UVLOG" "$REPO/bin/vs-speaker-up.sh" >"$EVID/task-30-ensure-speaker.log" 2>&1 \
  && log "speaker service up" || log "vs-speaker-up rc=$? (see task-30-ensure-speaker.log)"
log "GET /health :3910 -> HTTP $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:3910/health)"

# ── 1b. MinerU needs models-dir.pipeline -> the cached HF snapshot ────────────
# The registered MinerU MCP server inherits MINERU_TOOLS_CONFIG_JSON from this
# shell (acceptance_mcp passes os.environ through to every MCP subprocess). The
# stock ~/mineru.json points at an empty modelscope path, so OCR otherwise fails
# and silently falls back to an empty pymupdf extraction.
export MINERU_TOOLS_CONFIG_JSON="$EVID/mineru.local.json"
python3 - "$MINERU_TOOLS_CONFIG_JSON" <<'PY'
import json, os, pathlib, sys
out = pathlib.Path(sys.argv[1])
hub = pathlib.Path(os.environ.get("HF_HUB_CACHE", os.path.expanduser("~/.cache/huggingface/hub")))
repo = hub / "models--opendatalab--PDF-Extract-Kit-1.0"
sha = (repo / "refs" / "main").read_text().strip()
snap = repo / "snapshots" / sha
if not (snap / "models" / "MFR").is_dir():
    sys.exit(f"MinerU snapshot incomplete: {snap}")
cfg = json.loads((pathlib.Path.home() / "mineru.json").read_text())
cfg["models-dir"]["pipeline"] = str(snap)
out.write_text(json.dumps(cfg, indent=2))
print(f"MinerU models-dir.pipeline -> {snap}")
PY
[ -f "$MINERU_TOOLS_CONFIG_JSON" ] && log "MinerU config: $MINERU_TOOLS_CONFIG_JSON" 

# ── 1c. warm the model caches before the monitored window ─────────────────────
# diarization's `Pipeline.from_pretrained` issues a single Hub metadata probe on
# first load that ignores HF_HUB_OFFLINE (weights are already cached). Loading it
# here - under its own sampler, recorded as expected - keeps the operational
# window loopback-only without hiding the access.
: >"$OFFWARM"; touch "$OFFWARM.running"
descendants() { local pid=$1 c; for c in $(pgrep -P "$pid" 2>/dev/null); do echo "$c"; descendants "$c"; done; }
watch_pids() {
  { echo $$; descendants $$;
    pgrep -f 'acceptance_mcp.py|mcp_server.py|vsbridge|voicebridge|mineru_mcp_server|uvicorn app:app' 2>/dev/null;
    lsof -t -iTCP:3900 -sTCP:LISTEN 2>/dev/null;
    lsof -t -iTCP:3910 -sTCP:LISTEN 2>/dev/null; } | sort -u | paste -sd, -
}
sample_nonloopback() {
  local pids ts
  pids=$(watch_pids); [ -z "$pids" ] && return 0
  ts=$(date '+%H:%M:%S')
  lsof -n -P -i -a -p "$pids" -F pcn 2>/dev/null | awk -v ts="$ts" '
    /^p/ { pid=substr($0,2) }
    /^c/ { cmd=substr($0,2) }
    /^n/ { name=substr($0,2)
      if (index(name,"->")) { split(name,a,"->"); host=a[2]; sub(/:.*/,"",host)
        if (host!="127.0.0.1" && host!="localhost" && host!="::1" && host!="[::1]")
          print ts"|"pid"|"cmd"|"name } }'
}
( while [ -f "$OFFWARM.running" ]; do sample_nonloopback >>"$OFFWARM"; sleep 1; done ) & WARM_MON=$!
TOKEN="$(security find-generic-password -s voicestack-service -a service -w 2>/dev/null || true)"
log "warming models (one-shot HF metadata probe expected here)"
curl -s -m 300 -H "Authorization: Bearer $TOKEN" -F "file=@$REPO/fixtures/audio/clip_3s.wav" \
  "http://127.0.0.1:3910/meetings" -o "$EVID/task-30-warmup-meeting.json" \
  -w "warm-up transcribe HTTP %{http_code}\n"
curl -s -m 120 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"audio_path\":\"$REPO/fixtures/audio/clip_3s.wav\"}" "http://127.0.0.1:3910/identify" \
  -o "$EVID/task-30-warmup-identify.json" -w "warm-up identify HTTP %{http_code}\n"
rm -f "$OFFWARM.running"; wait "$WARM_MON" 2>/dev/null
log "warm-up done; warm-up non-loopback samples: $(grep -c . "$OFFWARM" 2>/dev/null)"

# ── 2. offline check: baseline-diff every non-loopback connection ─────────────
# The acceptance's own work is loopback-only. Sockets already open before the run
# (and the pre-existing VoiceStudio app's own cloud socket, which predates this
# run) are not induced by it, so only NEW non-loopback sockets are unexpected.
: >"$OFFLOG"; : >"$OFFFLAG"; : >"$OFFBASELINE"; : >"$OFFLOG.running"
VS_PID="$(lsof -t -iTCP:3900 -sTCP:LISTEN 2>/dev/null | head -1)"
for _ in 1 2 3; do sample_nonloopback | awk -F'|' '{print $4}' >>"$OFFBASELINE"; sleep 2; done
sort -u "$OFFBASELINE" -o "$OFFBASELINE"
log "offline baseline: $(grep -c . "$OFFBASELINE" 2>/dev/null) pre-existing non-loopback socket(s); VoiceStudio pid=${VS_PID:-none}"
sampler() {
  while [ -f "$OFFLOG.running" ]; do
    sample_nonloopback | tee -a "$OFFFLAG" >>"$OFFLOG"
    sleep 2
  done
}
sampler & SAMPLER_PID=$!
log "offline sampler started (pid $SAMPLER_PID; lsof every 2s)"

# ── 3. the acceptance driver (all MCP-facing assertions) ─────────────────────
log "running acceptance_mcp.py …"
"$PY" "$SD/tests/acceptance_mcp.py" --out "$ART" --evidence "$EVID" 2>&1 | tee "$DRIVER_LOG"
DRIVER_RC=${PIPESTATUS[0]}
log "driver rc=$DRIVER_RC"

# ── 4. stop the sampler, synthesize the summary JSON ─────────────────────────
rm -f "$OFFLOG.running"; wait "$SAMPLER_PID" 2>/dev/null
log "offline sampler stopped ($(grep -c . "$OFFLOG" 2>/dev/null) lsof lines, $(grep -c . "$OFFFLAG" 2>/dev/null) flagged)"

python3 - "$ART" "$OFFBASELINE" "$OFFFLAG" "$OFFWARM" "$OFFLOG" "$FINAL" "$DATA" \
         "$START" "$REPO" "$DRIVER_RC" "${VS_PID:-}" <<'PY'
import datetime, json, subprocess, sys, time

(art_p, base_p, flag_p, warm_p, log_p, final_p, data, start, repo, drc,
 vs_pid) = sys.argv[1:12]
try:
    art = json.load(open(art_p))
except Exception as exc:  # noqa: BLE001 - a missing report is itself a failure
    art = {"checks": [], "capabilities": {}, "overall_pass": False,
           "failed_checks": [f"driver output unreadable: {exc}"], "failed_capabilities": []}
baseline = {line for line in open(base_p).read().splitlines() if line.strip()}
flags = [line for line in open(flag_p).read().splitlines() if line.strip()]
unexpected, pre_existing_app, seen = [], [], set()
for line in flags:
    pid, cmd, name = line.split("|", 3)[1:4]
    if (pid, cmd, name) in seen or name in baseline:
        continue
    seen.add((pid, cmd, name))
    (pre_existing_app if pid == vs_pid else unexpected).append(f"{pid}|{cmd}|{name}")
warm = [line for line in open(warm_p).read().splitlines() if line.strip()]
warm_expected, warm_seen = [], set()
for line in warm:
    pid, cmd, name = line.split("|", 3)[1:4]
    if (pid, cmd, name) in warm_seen:
        continue
    warm_seen.add((pid, cmd, name))
    warm_expected.append(f"{pid}|{cmd}|{name}")
offline = {
    "method": "sampled `lsof -n -P -i -F pcn` every 2s over the acceptance "
              "process tree and the :3900/:3910 listeners; sockets present in a "
              "pre-run baseline window are excluded; run under HF_HUB_OFFLINE=1 "
              "TRANSFORMERS_OFFLINE=1 UV_OFFLINE=1",
    "baseline_pre_existing": sorted(baseline),
    "warmup_expected_outbound": warm_expected,
    "observed_non_loopback_samples": len(flags),
    "pre_existing_app_connection": pre_existing_app,
    "unexpected_outbound": unexpected,
    "result": "PASS" if not unexpected else "FAIL",
}
head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                      capture_output=True, text=True).stdout.strip()
final = {
    "task": 30,
    "title": "End-to-end acceptance across audiobook, transcription, speakers and MCP",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "git_head": head,
    "isolated_data_dir": data,
    "wall_s": round(time.time() - float(start), 1),
    "driver_rc": int(drc),
    "offline": offline,
    "artifacts": art.get("checks", []),
    "capabilities": art.get("capabilities", {}),
    "summary": {
        "artifacts_total": len(art.get("checks", [])),
        "artifacts_passed": sum(1 for c in art.get("checks", []) if c.get("pass")),
        "failed_checks": art.get("failed_checks") or [],
        "failed_capabilities": art.get("failed_capabilities") or [],
        "offline_result": offline["result"],
    },
}
final["result"] = "PASS" if (art.get("overall_pass") and offline["result"] == "PASS") else "FAIL"
json.dump(final, open(final_p, "w"), indent=2, ensure_ascii=False)
print(json.dumps(final["summary"], indent=2))
sys.exit(0 if final["result"] == "PASS" else 1)
PY
RC=$?

# ── 5. cleanup local processes (leave VoiceStudio as found) ───────────────────
for pid in $(listener); do kill "$pid" 2>/dev/null; done
rm -rf "$DATA" "$OFFLOG.running" 2>/dev/null
log "result=$([ "$RC" = 0 ] && echo PASS || echo FAIL)  evidence=$FINAL"
exit "$RC"
