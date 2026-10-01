#!/bin/bash
# vs-speaker-up.sh — on-demand speaker-service launcher (:3910)
#
# Checks whether the speaker-service is serving GET /health on :3910.
# If already up → exits 0 immediately (< 2 s).
# If down      → starts `uvicorn app:app` detached from the service venv and
#                polls /health until it responds (≤ VS_SPEAKER_TIMEOUT s).
#
# This script is NOT a keepalive/launchd agent and does NOT use launchd socket
# activation (a plain uvicorn cannot bind a launchd-owned socket). The MCP
# server runs it before every tool call because the service may have idle-exited.
#
# ENV OVERRIDES:
#   VS_SPEAKER_DIR      — service directory (default: ~/voicestack/speaker-service)
#   VS_SPEAKER_PORT     — HTTP port (default: 3910)
#   VS_SPEAKER_BIND     — bind address (default: 127.0.0.1)
#   VS_SPEAKER_VENV     — venv holding uvicorn (default: <dir>/.venv)
#   VS_SPEAKER_LOG      — uvicorn log file (default: <dir>/.uvicorn.log)
#   VS_SPEAKER_TIMEOUT  — launch timeout in seconds (default: 60)
#   VS_SPEAKER_POLL_INT — poll interval in seconds (default: 1)

set -euo pipefail

VS_SPEAKER_DIR="${VS_SPEAKER_DIR:-$HOME/voicestack/speaker-service}"
VS_SPEAKER_PORT="${VS_SPEAKER_PORT:-3910}"
VS_SPEAKER_BIND="${VS_SPEAKER_BIND:-127.0.0.1}"
VS_SPEAKER_VENV="${VS_SPEAKER_VENV:-$VS_SPEAKER_DIR/.venv}"
VS_SPEAKER_LOG="${VS_SPEAKER_LOG:-$VS_SPEAKER_DIR/.uvicorn.log}"
VS_SPEAKER_TIMEOUT="${VS_SPEAKER_TIMEOUT:-60}"
VS_SPEAKER_POLL_INT="${VS_SPEAKER_POLL_INT:-1}"
HEALTH_URL="http://127.0.0.1:${VS_SPEAKER_PORT}/health"

# ── helpers ───────────────────────────────────────────────────────────────────
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }
log_err() { log "ERROR: $*" >&2; }

probe() { curl -s --max-time 2 --fail "$HEALTH_URL" > /dev/null 2>&1; }

# ── lock (safe concurrent re-runs) ───────────────────────────────────────────
LOCK_DIR="/tmp/vs-speaker-up.lock.${VS_SPEAKER_PORT}"
LOCK_HELD=0
finish() { if [ "$LOCK_HELD" = 1 ]; then rm -rf "$LOCK_DIR"; fi; }
trap finish EXIT

# Self-heal a lock left by a SIGKILLed launcher: if no launcher process is alive
# to own it, reclaim the directory instead of waiting out the full timeout.
if [ -d "$LOCK_DIR" ] && ! pgrep -f "vs-speaker-up.sh" > /dev/null 2>&1; then
  log "reclaiming stale lock ${LOCK_DIR}"
  rm -rf "$LOCK_DIR"
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "another launcher holds the lock, waiting for :${VS_SPEAKER_PORT}…"
  for i in $(seq 1 "$VS_SPEAKER_TIMEOUT"); do
    if probe; then
      log "service came up via concurrent launch (waited ${i}s)"
      exit 0
    fi
    sleep 1
  done
  log_err "concurrent launch timed out after ${VS_SPEAKER_TIMEOUT}s"
  exit 1
fi
LOCK_HELD=1

# ── fast path: already up ─────────────────────────────────────────────────────
if probe; then
  log "service already up on :${VS_SPEAKER_PORT}, nothing to do"
  exit 0
fi

# ── cold path: start uvicorn from the service venv ───────────────────────────
UVICORN="$VS_SPEAKER_VENV/bin/uvicorn"
if [ ! -x "$UVICORN" ]; then
  log_err "uvicorn not found at '${UVICORN}' (set VS_SPEAKER_VENV)"
  exit 1
fi
if [ ! -f "$VS_SPEAKER_DIR/app.py" ]; then
  log_err "app.py not found in '${VS_SPEAKER_DIR}' (set VS_SPEAKER_DIR)"
  exit 1
fi

log "service down on :${VS_SPEAKER_PORT}, starting uvicorn…"
cd "$VS_SPEAKER_DIR"
nohup "$UVICORN" app:app --host "$VS_SPEAKER_BIND" --port "$VS_SPEAKER_PORT" \
  >> "$VS_SPEAKER_LOG" 2>&1 < /dev/null &
disown 2>/dev/null || true

elapsed=0
while (( elapsed < VS_SPEAKER_TIMEOUT )); do
  sleep "$VS_SPEAKER_POLL_INT"
  elapsed=$(( elapsed + VS_SPEAKER_POLL_INT ))
  if probe; then
    log "service up after ~${elapsed}s"
    exit 0
  fi
done

log_err "service did not respond within ${VS_SPEAKER_TIMEOUT}s; see ${VS_SPEAKER_LOG}"
exit 1
