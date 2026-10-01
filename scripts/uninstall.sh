#!/bin/bash
# uninstall.sh — rollback / uninstall for the local VoiceStudio + VoiceStack stack.
#
# This is the inverse of bin/vs-ensure-up.sh (VoiceStudio :3900),
# bin/vs-speaker-up.sh (:3910) and bin/vs-speaker-always-on.sh (opt-in launchd).
#
# DEFAULT RUN ("rollback", safe, NON-DESTRUCTIVE):
#   1. take a timestamped backup of the speaker DB (before anything else),
#   2. quit VoiceStudio (osascript, pkill fallback),
#   3. bootout any installed voicestack/voicestudio launchd agents,
#   4. free :3900 and :3910.
#   It does NOT delete the app or its weights — the install stays usable.
#
# FULL REMOVAL adds `--remove-app` (opt-in, destructive):
#   5. rm -rf /Applications/VoiceStudio.app
#   6. rm -rf "~/Library/Application Support/VoiceStudio"   (VoiceStudio's OWN runtime/weights)
#   It NEVER touches the shared HF cache ~/.cache/huggingface/hub, which MinerU and
#   mlx-whisper reuse. It also never deletes the speaker DB (VoiceStudioStack), only
#   backs it up.
#
# USAGE:
#   scripts/uninstall.sh                 # backup + stop + unload agents + free ports
#   scripts/uninstall.sh --dry-run       # print every planned action, change NOTHING
#   scripts/uninstall.sh --backup        # only take a timestamped DB backup, then exit
#   scripts/uninstall.sh --remove-app    # the full removal (app + own weights) + the above
#   scripts/uninstall.sh --remove-app --dry-run   # show the rm commands without running them
#   scripts/uninstall.sh --yes           # skip the confirmation prompt
#   scripts/uninstall.sh --help
#
# ENV OVERRIDES:
#   VS_APP_PATH        — .app bundle (default: /Applications/VoiceStudio.app)
#   VS_APP_SUPPORT     — VoiceStudio Application Support dir (default: ~/Library/Application Support/VoiceStudio)
#   VS_STACK_DIR       — VoiceStack data dir (default: ~/Library/Application Support/VoiceStudioStack)
#   VS_DB              — speaker DB (default: <stack>/voicestack.db)
#   VS_BACKUP_DIR      — backup destination (default: <stack>/backups)
#   VS_PORT            — VoiceStudio backend port (default: 3900)
#   VS_SPEAKER_PORT    — speaker-service port (default: 3910)
#   VS_AGENT_PREFIXES  — space-separated launchd label prefixes to bootout
#                        (default: "com.voicestack. sh.voicestack. com.voicestudio.")
#
# RESTORE PATH (documented, see also --help footer):
#   # stop the service first
#   ~/voicestack/scripts/uninstall.sh --backup        # optional: snapshot current state
#   kill $(lsof -t -iTCP:3910 -sTCP:LISTEN) 2>/dev/null || true
#   sqlite3 "$DB" ".restore '$BACKUP_DIR/voicestack-<YYYYmmdd-HHMMSS>.db'"
#   #   (equivalent, service stopped: cp "$BACKUP_DIR/<backup>.db" "$DB")
#   ~/voicestack/bin/vs-speaker-up.sh                  # bring the service back

set -uo pipefail

# ── config ───────────────────────────────────────────────────────────────────
VS_APP_PATH="${VS_APP_PATH:-/Applications/VoiceStudio.app}"
VS_APP_SUPPORT="${VS_APP_SUPPORT:-$HOME/Library/Application Support/VoiceStudio}"
VS_STACK_DIR="${VS_STACK_DIR:-$HOME/Library/Application Support/VoiceStudioStack}"
VS_DB="${VS_DB:-$VS_STACK_DIR/voicestack.db}"
VS_BACKUP_DIR="${VS_BACKUP_DIR:-$VS_STACK_DIR/backups}"
VS_PORT="${VS_PORT:-3900}"
VS_SPEAKER_PORT="${VS_SPEAKER_PORT:-3910}"
VS_AGENT_PREFIXES="${VS_AGENT_PREFIXES:-com.voicestack. sh.voicestack. com.voicestudio.}"
SHARED_HF="${SHARED_HF_CACHE:-$HOME/.cache/huggingface/hub}"
DOMAIN="gui/$(id -u)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

DRY_RUN=0
REMOVE_APP=0
BACKUP_ONLY=0
ASSUME_YES=0

# ── helpers ──────────────────────────────────────────────────────────────────
log()     { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }
log_err() { log "ERROR: $*" >&2; }
# run: echo + execute, or echo only under --dry-run
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  DRY-RUN: %s\n' "$*"
    return 0
  fi
  printf '  RUN: %s\n' "$*"
  "$@"
}

print_usage_header() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$1"
}

usage() {
  print_usage_header "$0"
  cat <<'EOF'

REMOVAL COMMANDS (run only with --remove-app, omitted under --dry-run):
  rm -rf "/Applications/VoiceStudio.app"
  rm -rf "$HOME/Library/Application Support/VoiceStudio"
PRESERVED (never removed):
  $HOME/.cache/huggingface/hub        (shared HF cache: MinerU + mlx-whisper)
  $HOME/Library/Application Support/VoiceStudioStack/voicestack.db  (speaker DB, backed up only)

BACKUP PATH CONVENTION:
  $HOME/Library/Application Support/VoiceStudioStack/backups/voicestack-<YYYYmmdd-HHMMSS>.db
EOF
}

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  [ "$DRY_RUN" = 1 ] && return 0
  printf 'Proceed with uninstall%s? [y/N] ' "$([ "$REMOVE_APP" = 1 ] && echo ' AND app removal')"
  read -r reply
  case "$reply" in
    y|Y|yes|YES) return 0 ;;
    *) log "aborted by user"; exit 0 ;;
  esac
}

# ── 1. backup the speaker DB (WAL-safe) ──────────────────────────────────────
do_backup() {
  log "backing up speaker DB"
  if [ ! -f "$VS_DB" ]; then
    log "  no DB at '$VS_DB' — nothing to back up"
    return 0
  fi
  local ts dest
  ts="$(date '+%Y%m%d-%H%M%S')"
  dest="$VS_BACKUP_DIR/voicestack-${ts}.db"
  run mkdir -p "$VS_BACKUP_DIR"
  # sqlite3 .backup is WAL-safe and captures a consistent snapshot.
  # NOTE: values are interpolated into the dot-command, so quotes matter.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  DRY-RUN: sqlite3 %q ".backup %q"\n' "$VS_DB" "$dest"
  else
    if ! sqlite3 "$VS_DB" ".backup '$dest'"; then
      log_err "sqlite3 .backup failed; falling back to a plain copy"
      cp -p "$VS_DB" "$dest" || { log_err "backup copy failed"; return 1; }
    fi
    if [ -f "$dest" ]; then
      local check
      check="$(sqlite3 "$dest" 'PRAGMA integrity_check;' 2>&1 || echo 'integrity_check failed')"
      log "  backup written: $dest ($(du -h "$dest" | cut -f1)); PRAGMA integrity_check = $check"
      printf '%s\n' "$dest"
    fi
  fi
}

# ── 2. quit VoiceStudio + the runtime backend ────────────────────────────────
quit_voicestudio() {
  log "quitting VoiceStudio"
  if pgrep -f "$VS_APP_PATH/Contents/MacOS/VoiceStudio" >/dev/null 2>&1; then
    run osascript -e 'quit app "VoiceStudio"' 2>/dev/null || true
    if [ "$DRY_RUN" = 0 ]; then
      local i
      for i in $(seq 1 5); do
        pgrep -f "$VS_APP_PATH/Contents/MacOS/VoiceStudio" >/dev/null 2>&1 || break
        sleep 1
      done
    fi
    # Fallback if the graceful quit did not take.
    if [ "$DRY_RUN" = 0 ] && pgrep -f "$VS_APP_PATH/Contents/MacOS/VoiceStudio" >/dev/null 2>&1; then
      log "  graceful quit timed out; pkill fallback"
      run pkill -f "$VS_APP_PATH/Contents/MacOS/VoiceStudio" || true
      sleep 1
    fi
  else
    log "  VoiceStudio app not running"
  fi
}

# ── 3. bootout opt-in launchd agents ─────────────────────────────────────────
label_matches() {
  local label="$1" p
  for p in $VS_AGENT_PREFIXES; do
    case "$label" in "$p"*) return 0 ;; esac
  done
  return 1
}

unload_agents() {
  log "unloading voicestack/voicestudio launchd agents"
  local labels label plist base
  labels=""
  # (a) labels currently loaded in launchd
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    if label_matches "$label"; then labels="$labels $label"; fi
  done < <(launchctl list 2>/dev/null | awk '{print $3}' | grep -v '^-$' || true)
  # (b) plist files on disk (covers a loaded-but-listed variant and an unloaded plist)
  if [ -d "$LAUNCH_AGENTS_DIR" ]; then
    for plist in "$LAUNCH_AGENTS_DIR"/*.plist; do
      [ -e "$plist" ] || continue
      base="$(basename "$plist" .plist)"
      if label_matches "$base"; then labels="$labels $base"; fi
    done
  fi
  labels="$(printf '%s\n' $labels | sort -u)"
  if [ -z "$labels" ]; then
    log "  no matching launchd agents installed"
    return 0
  fi
  for label in $labels; do
    log "  bootout $DOMAIN/$label"
    run launchctl bootout "$DOMAIN/$label" || true
    plist="$LAUNCH_AGENTS_DIR/$label.plist"
    if [ -f "$plist" ]; then run rm -f "$plist"; fi
  done
  if [ "$DRY_RUN" = 0 ]; then
    local i leftover
    for i in $(seq 1 10); do
      leftover=""
      for label in $labels; do
        launchctl print "$DOMAIN/$label" >/dev/null 2>&1 && leftover="$leftover $label"
      done
      [ -z "$leftover" ] && break
      sleep 1
    done
    [ -n "$leftover" ] && log_err "agents still loaded:$leftover"
  fi
}

# ── 4. free :3900 / :3910 ────────────────────────────────────────────────────
free_port() {
  local port="$1" pids pid
  pids="$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -z "$pids" ]; then
    log "  :$port already free"
    return 0
  fi
  for pid in $pids; do
    log "  :$port held by pid $pid — terminating"
    run kill -TERM "$pid" || true
  done
  if [ "$DRY_RUN" = 0 ]; then
    local i
    for i in $(seq 1 5); do
      pids="$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
      [ -z "$pids" ] && break
      sleep 1
    done
    for pid in $pids; do
      log "  pid $pid ignored SIGTERM — SIGKILL"
      run kill -KILL "$pid" || true
    done
  fi
}

free_ports() {
  log "freeing :$VS_PORT and :$VS_SPEAKER_PORT"
  free_port "$VS_PORT"
  free_port "$VS_SPEAKER_PORT"
}

# ── 5. remove the app + VoiceStudio's OWN weights (opt-in) ───────────────────
remove_files() {
  # Safety: never operate on root/empty or the shared HF cache.
  case "$VS_APP_PATH" in
    ""|"/"|"/Applications") log_err "refusing to remove unsafe app path '$VS_APP_PATH'"; return 1 ;;
  esac
  case "$VS_APP_SUPPORT" in
    ""|"/"|"$HOME"|"$HOME/Library") log_err "refusing to remove unsafe support path '$VS_APP_SUPPORT'"; return 1 ;;
  esac
  log "removing app + VoiceStudio's own runtime/weights"
  if [ -e "$VS_APP_PATH" ]; then
    run rm -rf "$VS_APP_PATH"
  else
    log "  $VS_APP_PATH already absent"
  fi
  if [ -e "$VS_APP_SUPPORT" ]; then
    run rm -rf "$VS_APP_SUPPORT"
  else
    log "  $VS_APP_SUPPORT already absent"
  fi
  log "  NOT touching shared HF cache: $SHARED_HF"
}

# ── main ─────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=1 ;;
    --remove-app) REMOVE_APP=1 ;;
    --backup)     BACKUP_ONLY=1 ;;
    --yes|-y)     ASSUME_YES=1 ;;
    --help|-h)    usage; exit 0 ;;
    *) log_err "unknown flag '$1'"; usage; exit 2 ;;
  esac
  shift
done

log "uninstall.sh starting (dry-run=$DRY_RUN remove-app=$REMOVE_APP)"

do_backup
if [ "$BACKUP_ONLY" = 1 ]; then
  log "backup-only requested; done"
  exit 0
fi

confirm

quit_voicestudio
unload_agents
free_ports

if [ "$REMOVE_APP" = 1 ]; then
  remove_files
else
  log "app/weights kept (pass --remove-app for full removal)"
fi

log "preserved shared HF cache: $SHARED_HF"
if [ "$DRY_RUN" = 1 ]; then
  log "dry-run complete: nothing was changed"
else
  log "uninstall complete"
fi
