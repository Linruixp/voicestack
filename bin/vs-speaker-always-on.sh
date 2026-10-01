#!/bin/bash
# vs-speaker-always-on.sh — OPT-IN always-on variant for the speaker-service.
#
# The DEFAULT mode is on-demand: the stdio MCP / vs-web.sh start uvicorn via
# vs-speaker-up.sh when needed and the service exits after VASTACK_IDLE_EXIT_S
# idle seconds. This script is the OPT-IN escape hatch: it installs a per-user
# launchd agent with KeepAlive so :3910 is always resident. It is NEVER
# installed by default and nothing on the on-demand path needs it.
#
# The agent pins VASTACK_IDLE_EXIT_S=0; otherwise the idle watchdog would exit
# the service and launchd would restart it every ~10 min.
#
# NO launchd socket activation: a plain uvicorn cannot bind a launchd-owned
# socket without launch_activate_socket(), which fails with EADDRINUSE.
#
# USAGE:
#   vs-speaker-always-on.sh install     # render plist + launchctl bootstrap
#   vs-speaker-always-on.sh status      # launchctl print + /health probe
#   vs-speaker-always-on.sh uninstall   # bootout + remove the plist
#   vs-speaker-always-on.sh plist       # print the rendered plist (no install)
#
# ENV OVERRIDES:
#   VS_SPEAKER_DIR     — service directory (default: ~/voicestack/speaker-service)
#   VS_SPEAKER_PORT    — HTTP port (default: 3910)
#   VS_SPEAKER_BIND    — bind address (default: 127.0.0.1)
#   VS_SPEAKER_VENV    — venv holding uvicorn (default: <dir>/.venv)
#   VS_SPEAKER_LOG     — stdout/stderr log (default: <dir>/.uvicorn.log)
#   VS_ALWAYS_ON_LABEL — launchd label (default: com.voicestack.speaker-service)
#   VS_ALWAYS_ON_WAIT  — /health wait after install, seconds (default: 30)

set -euo pipefail

VS_SPEAKER_DIR="${VS_SPEAKER_DIR:-$HOME/voicestack/speaker-service}"
VS_SPEAKER_PORT="${VS_SPEAKER_PORT:-3910}"
VS_SPEAKER_BIND="${VS_SPEAKER_BIND:-127.0.0.1}"
VS_SPEAKER_VENV="${VS_SPEAKER_VENV:-$VS_SPEAKER_DIR/.venv}"
VS_SPEAKER_LOG="${VS_SPEAKER_LOG:-$VS_SPEAKER_DIR/.uvicorn.log}"
VS_ALWAYS_ON_LABEL="${VS_ALWAYS_ON_LABEL:-com.voicestack.speaker-service}"
VS_ALWAYS_ON_WAIT="${VS_ALWAYS_ON_WAIT:-30}"

DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/${VS_ALWAYS_ON_LABEL}.plist"
HEALTH_URL="http://127.0.0.1:${VS_SPEAKER_PORT}/health"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }
log_err() { log "ERROR: $*" >&2; }

render_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${VS_ALWAYS_ON_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VS_SPEAKER_VENV}/bin/uvicorn</string>
    <string>app:app</string>
    <string>--host</string>
    <string>${VS_SPEAKER_BIND}</string>
    <string>--port</string>
    <string>${VS_SPEAKER_PORT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${VS_SPEAKER_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>VASTACK_IDLE_EXIT_S</key>
    <string>0</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${VS_SPEAKER_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${VS_SPEAKER_LOG}</string>
</dict>
</plist>
EOF
}

wait_health() {
  local elapsed=0
  until curl -s --max-time 2 --fail "$HEALTH_URL" > /dev/null 2>&1; do
    if (( elapsed >= VS_ALWAYS_ON_WAIT )); then
      return 1
    fi
    sleep 1
    elapsed=$(( elapsed + 1 ))
  done
  return 0
}

cmd_install() {
  if [ ! -x "${VS_SPEAKER_VENV}/bin/uvicorn" ]; then
    log_err "uvicorn not found at '${VS_SPEAKER_VENV}/bin/uvicorn'"
    exit 1
  fi
  mkdir -p "$(dirname "$PLIST")"
  render_plist > "$PLIST"
  plutil -lint "$PLIST" > /dev/null
  launchctl bootout "${DOMAIN}/${VS_ALWAYS_ON_LABEL}" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  log "installed ${PLIST}; waiting for ${HEALTH_URL}…"
  if wait_health; then
    log "always-on agent is serving :${VS_SPEAKER_PORT} (VASTACK_IDLE_EXIT_S=0)"
    return 0
  fi
  log_err "agent bootstrapped but /health did not answer within ${VS_ALWAYS_ON_WAIT}s"
  log_err "check ${VS_SPEAKER_LOG} and: launchctl print ${DOMAIN}/${VS_ALWAYS_ON_LABEL}"
  exit 1
}

cmd_uninstall() {
  launchctl bootout "${DOMAIN}/${VS_ALWAYS_ON_LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  local i=0
  while launchctl print "${DOMAIN}/${VS_ALWAYS_ON_LABEL}" > /dev/null 2>&1; do
    i=$(( i + 1 ))
    if (( i >= 10 )); then
      log_err "agent still loaded ${i}s after bootout; inspect launchctl print"
      exit 1
    fi
    sleep 1
  done
  log "uninstalled ${VS_ALWAYS_ON_LABEL}; :${VS_SPEAKER_PORT} is no longer kept alive"
}

cmd_status() {
  if launchctl print "${DOMAIN}/${VS_ALWAYS_ON_LABEL}" > /dev/null 2>&1; then
    log "launchd agent loaded: ${DOMAIN}/${VS_ALWAYS_ON_LABEL}"
  else
    log "launchd agent NOT loaded: ${DOMAIN}/${VS_ALWAYS_ON_LABEL}"
  fi
  lsof -nP -iTCP:"${VS_SPEAKER_PORT}" -sTCP:LISTEN || log "no listener on :${VS_SPEAKER_PORT}"
  curl -s --max-time 2 "$HEALTH_URL" || log "no /health response"
  echo
}

case "${1:-}" in
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  plist) render_plist ;;
  *)
    echo "usage: $0 {install|uninstall|status|plist}" >&2
    exit 2
    ;;
esac
