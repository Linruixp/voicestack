#!/bin/bash
# vs-ensure-up.sh — on-demand VoiceStudio backend launcher
#
# Checks whether VoiceStudio's backend is serving the discovery doc on :3900.
# If already up → exits 0 immediately (< 2 s).
# If down      → launches the app and polls until the doc returns (≤ 90 s).
# If absent    → prints an error to stderr and exits non-zero.
#
# This script is NOT installed as a keepalive / launchd agent.
# To opt into always-on: save the commented LaunchAgent plist below and
# `launchctl load ~/Library/LaunchAgents/com.voicestudio.keepalive.plist`
#
# ENV OVERRIDES:
#   VS_APP_PATH   — path to the .app bundle (default: /Applications/VoiceStudio.app)
#   VS_PORT       — backend port (default: 3900)
#   VS_DISCOVERY  — discovery doc path (default: /.well-known/voicestudio-speech)
#   VS_TIMEOUT    — launch timeout in seconds (default: 90)
#   VS_POLL_INT   — poll interval in seconds (default: 1)

set -euo pipefail

# ── config ────────────────────────────────────────────────────────────────────
VS_APP_PATH="${VS_APP_PATH:-/Applications/VoiceStudio.app}"
VS_PORT="${VS_PORT:-3900}"
VS_DISCOVERY="${VS_DISCOVERY:-/.well-known/voicestudio-speech}"
VS_TIMEOUT="${VS_TIMEOUT:-90}"
VS_POLL_INT="${VS_POLL_INT:-1}"
DISCO_URL="http://127.0.0.1:${VS_PORT}${VS_DISCOVERY}"

# ── helpers ───────────────────────────────────────────────────────────────────
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }
log_err() { log "ERROR: $*" >&2; }

# curl helper: exit 0 + output if doc returns, exit non-zero otherwise
probe() {
  curl -s --max-time 2 --fail "$DISCO_URL" > /dev/null 2>&1
}

# ── lock (safe concurrent re-runs) ───────────────────────────────────────────
LOCK_DIR="/tmp/vs-ensure-up.lock.${VS_PORT}"
finish() { rm -rf "$LOCK_DIR"; }
trap finish EXIT

# If another instance is already launching, wait for it instead of racing.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "another instance is launching, waiting for it…"
  # Wait for the lock holder to finish (they will clean up the dir).
  # We poll the discovery endpoint instead of relying on the lock file,
  # because the holder may exit before we look.
  for i in $(seq 1 "$VS_TIMEOUT"); do
    if probe; then
      log "backend came up via concurrent launch (waited ${i}s)"
      exit 0
    fi
    sleep 1
  done
  log_err "concurrent launch timed out after ${VS_TIMEOUT}s"
  exit 1
fi

# ── fast path: already up ─────────────────────────────────────────────────────
if probe; then
  log "backend already up on :${VS_PORT}, nothing to do"
  exit 0
fi

# ── cold path: app must be launched ──────────────────────────────────────────
if [[ ! -d "$VS_APP_PATH" ]]; then
  log_err "VoiceStudio app not found at '${VS_APP_PATH}'"
  log_err "VoiceStudio may not be installed. Please install it from https://..."
  exit 1
fi

log "backend down on :${VS_PORT}, launching VoiceStudio…"
open -a "$VS_APP_PATH"

# Poll until the discovery doc responds or we hit the timeout.
elapsed=0
while (( elapsed < VS_TIMEOUT )); do
  sleep "$VS_POLL_INT"
  elapsed=$(( elapsed + VS_POLL_INT ))
  if probe; then
    log "backend up after ~${elapsed}s"
    exit 0
  fi
done

# Timeout — give a helpful message.
log_err "backend did not respond within ${VS_TIMEOUT}s"
log_err "VoiceStudio's first-run onboarding may be incomplete or the runtime failed to start."
log_err "Check the app: open -a VoiceStudio"
exit 1
