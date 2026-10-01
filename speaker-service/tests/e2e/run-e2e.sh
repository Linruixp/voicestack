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
