#!/bin/bash
# run-e2e.sh — drive the five-view UI end-to-end against the REAL service.
#
# Starts a COLD service through bin/vs-web.sh (proving the launcher opens the
# UI with the service auto-started), using an isolated temp data dir so the
# operator's registry is untouched, then runs the Playwright click-through and
# captures all evidence (screenshots, keepalive log, loopback binding).
#
# Env: VS_EVIDENCE (default ~/.omo/evidence/voicestudio-omo-local-stack/task-25-ui)

set -euo pipefail

REPO="$HOME/voicestack"
SERVICE="$REPO/speaker-service"
EVID="${VS_EVIDENCE:-$HOME/.omo/evidence/voicestudio-omo-local-stack/task-25-ui}"
DATA="$(mktemp -d "${TMPDIR:-/tmp}/vs-ui-e2e-data.XXXXXX")"
LOG="$EVID/uvicorn-e2e.log"
mkdir -p "$EVID"

# The 2-voice fixtures cannot reach the product handoff default (3 unknown
# clusters), so lower it to 1: any unknown cluster opens a batch for the
# deep-link wizard scenario.
export VASTACK_HANDOFF_THRESHOLD=1

# Synthesise a voice absent from the fixtures so the wizard upload still leaves
# an unknown cluster after the earlier scenarios enrolled Alice.
VS_WIZARD_AUDIO="$EVID/wizard-voice.wav"
if command -v say >/dev/null 2>&1 && say -v Daniel -o "$VS_WIZARD_AUDIO" --data-format=LEI16@16000 \
  "This is an entirely new speaker, never enrolled in this system before. Please confirm that the speaker naming wizard can resolve this unknown voice cluster from the transcript." 2>/dev/null; then
  echo "== synthesised fresh wizard-voice fixture: $VS_WIZARD_AUDIO =="
else
  VS_WIZARD_AUDIO="$REPO/fixtures/audio/en_30s.wav"
  echo "WARN: 'say' unavailable; wizard scenario falls back to $VS_WIZARD_AUDIO" >&2
fi
export VS_WIZARD_AUDIO

cleanup() {
  pkill -f "uvicorn app:app --host 127.0.0.1 --port 3910" 2>/dev/null || true
}
trap cleanup EXIT

echo "== evidence: $EVID"
echo "== isolated data dir: $DATA"

# 0. Cold start: no listener, no service process.
pkill -f "uvicorn app:app --host 127.0.0.1 --port 3910" 2>/dev/null || true
sleep 1
if curl -s --max-time 2 http://127.0.0.1:3910/health > /dev/null; then
  echo "ERROR: a service was already listening on 3910" >&2; exit 1
fi
echo "== service confirmed down (cold start) =="

# 1. Launch via vs-web.sh (headless: no `open`), isolated data dir + log.
export VASTACK_DATA_DIR="$DATA"
export VS_SPEAKER_LOG="$LOG"
export VS_WEB_NO_OPEN=1
"$REPO/bin/vs-web.sh" > "$EVID/vs-web-coldstart.txt" 2>&1
cat "$EVID/vs-web-coldstart.txt"

# 2. Binding + loopback-only + token-absence evidence.
lsof -nP -iTCP:3910 -sTCP:LISTEN > "$EVID/listen-3910.txt" 2>&1 || true
cat "$EVID/listen-3910.txt"
TOKEN="$(security find-generic-password -s voicestack-service -a service -w)"
{
  echo "== GET / (cookie mint) =="; curl -s -D - -o /dev/null http://127.0.0.1:3910/
  echo "== served bytes token-scan =="
  for p in / /static/app.js /static/app.css; do
    n=$(curl -s "http://127.0.0.1:3910$p" | grep -c "$TOKEN" || true)
    echo "$p : token occurrences = $n"
  done
  echo "== LAN probe (must fail/refuse) =="
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [ -n "$LAN_IP" ]; then
    curl -s --max-time 3 "http://$LAN_IP:3910/health" && echo "UNEXPECTED: LAN reachable" || echo "LAN $LAN_IP:3910 refused (expected)"
  else
    echo "no en0 LAN IP; skipping LAN probe"
  fi
} > "$EVID/ui-security.txt" 2>&1
cat "$EVID/ui-security.txt"

# 3. Playwright click-through against the live service.
echo "== running Playwright e2e =="
cd "$SERVICE/tests/e2e"
VS_TOKEN="$TOKEN" VS_EVIDENCE="$EVID" node ui_e2e.mjs 2>&1 | tee "$EVID/playwright.txt"

# 4. Keepalive proof from the service access log.
echo "== keepalive (uvicorn access log GET /health) =="
grep "GET /health" "$LOG" | tail -20 | tee "$EVID/keepalive.txt" || echo "(no /health lines found)"

echo "== DONE. Data dir kept at $DATA (service stopped on exit) =="
