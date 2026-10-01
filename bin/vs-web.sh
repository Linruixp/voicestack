#!/bin/bash
# vs-web.sh — open the minimal local Web UI for the speaker-service.
#
# Ensures the speaker-service is up on 127.0.0.1:3910 (via vs-speaker-up.sh),
# waits for GET /health, then opens the UI root in the default browser. The UI
# mints its own same-origin session cookie on that first load; no token is ever
# passed to the browser.
#
# ENV OVERRIDES:
#   VS_SPEAKER_PORT   — service port (default: 3910)
#   VS_WEB_NO_OPEN    — set to 1 to skip `open` (headless/automated runs)
#   VS_WEB_TIMEOUT    — /health wait timeout in seconds (default: 60)

set -euo pipefail

VS_SPEAKER_PORT="${VS_SPEAKER_PORT:-3910}"
VS_WEB_NO_OPEN="${VS_WEB_NO_OPEN:-0}"
VS_WEB_TIMEOUT="${VS_WEB_TIMEOUT:-60}"
BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="http://127.0.0.1:${VS_SPEAKER_PORT}/"
HEALTH_URL="http://127.0.0.1:${VS_SPEAKER_PORT}/health"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }

# 1. Ensure the service is running (starts uvicorn detached if it was down).
"$BIN_DIR/vs-speaker-up.sh"

# 2. Wait until the UI root is actually answering (health is the readiness probe).
elapsed=0
until curl -s --max-time 2 --fail "$HEALTH_URL" > /dev/null 2>&1; do
  if (( elapsed >= VS_WEB_TIMEOUT )); then
    echo "ERROR: service did not answer ${HEALTH_URL} within ${VS_WEB_TIMEOUT}s" >&2
    exit 1
  fi
  sleep 1
  elapsed=$(( elapsed + 1 ))
done
log "speaker-service ready at ${URL}"

# 3. Open the UI (unless suppressed for automated runs).
if [ "$VS_WEB_NO_OPEN" = "1" ]; then
  log "VS_WEB_NO_OPEN=1, not launching a browser"
else
  open "$URL"
  log "opened ${URL}"
fi
