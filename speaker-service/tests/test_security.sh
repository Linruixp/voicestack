#!/usr/bin/env bash
# Security & privacy hardening verification (plan todo 27).
#
# Proves, against the live loopback service plus isolated in-process apps:
#   (a) no service/our-code binds 0.0.0.0
#   (b) mutating endpoints require auth (401 without a bearer token)
#   (c) CORS is disabled (no Access-Control-Allow-Origin on any probe)
#   (d) the app-data dir AND the SQLite DB are mode 0700
#   (e) the HF token lives only in the Keychain, never in the repo/.env/assets
#   (f) VASTACK_DELETE_AUDIO_AFTER_TRANSCRIBE deletes the upload after a job
#   (g) voiceprints are biometric data (FileVault documented in docs/privacy.md)
#   (h) DELETE /speakers/{id} purges the speaker's voiceprint rows
#   (i) the Web UI authenticates via the session cookie; the token is in NO asset
#
# Falsifiability: two NEGATIVE controls prove the bind check is real -- a temp
# source copy carrying `0.0.0.0` is detected by the static scanner, and a
# throwaway server bound to 0.0.0.0 is flagged by the lsof probe.
#
# Exit status 0 iff every assertion passes.

set -uo pipefail

REPO="${HOME}/voicestack"
SVC="${REPO}/speaker-service"
PY="${SVC}/.venv/bin/python"
BASE="http://127.0.0.1:3910"
DATA_DIR="${HOME}/Library/Application Support/VoiceStudioStack"
DB="${DATA_DIR}/voicestack.db"
UI_STATIC="${SVC}/ui_static"
KEYCHAIN_SVC="voicestack-service"
KEYCHAIN_SVC_ACCT="service"
KEYCHAIN_HF="voicestack-hf"

PASS=0
FAIL=0
ok() { printf 'PASS  %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf 'FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }
note() { printf 'INFO  %s\n' "$1"; }

ensure_up() {
  curl -s --max-time 2 --fail "${BASE}/health" >/dev/null 2>&1 && return 0
  "${REPO}/bin/vs-speaker-up.sh" >/dev/null 2>&1 || true
  local i
  for i in $(seq 1 30); do
    curl -s --max-time 2 --fail "${BASE}/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

wildcard_bind() { # $1 port -> prints "<addr>:<port>" when bound on a wildcard
  lsof -iTCP -sTCP:LISTEN -P -n 2>/dev/null | awk -v p="$1" \
    '$9 == "*:" p || $9 == "0.0.0.0:" p { print $9 }'
}
loopback_bind() { # $1 port -> prints bind when bound on loopback
  lsof -iTCP -sTCP:LISTEN -P -n 2>/dev/null | awk -v p="$1" \
    '$9 == "127.0.0.1:" p || $9 == "[::1]:" p { print $9 }'
}
# Scan only PRODUCTION sources (top-level service modules, scripts, launchers):
# the harness itself must mention the literal for its negative control.
code_wildcard_hits() { # scan the given *.py/*.sh files/dirs for the literal
  grep -rIF '0.0.0.0' --include='*.py' --include='*.sh' \
    --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=node_modules \
    "$@" 2>/dev/null
}
free_port() {
  "${PY}" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

echo "=== speaker-service security & privacy checks (todo 27) ==="
date -u '+started %Y-%m-%dT%H:%M:%SZ'
echo

# ── (a) loopback-only binds, no 0.0.0.0 anywhere in our code ──────────────────
echo "--- (a) no service or code binds 0.0.0.0 ---"
if ensure_up; then
  if [ -n "$(wildcard_bind 3910)" ]; then
    bad "(a) our service 3910 is bound on a WILDCARD address: $(wildcard_bind 3910)"
  elif [ -n "$(loopback_bind 3910)" ]; then
    ok "(a) service 3910 listens on loopback only ($(loopback_bind 3910))"
  else
    bad "(a) service 3910 is not listening on loopback"
  fi
else
  bad "(a) service on ${BASE} never became healthy"
fi
for p in 3900 3911; do
  if [ -n "$(wildcard_bind "$p")" ]; then
    bad "(a) port ${p} is bound on a WILDCARD address: $(wildcard_bind "$p")"
  else
    ok "(a) port ${p}: no wildcard listener"
  fi
done
if [ -z "$(code_wildcard_hits "${SVC}"/*.py "${SVC}/scripts"/*.py "${REPO}/bin"/*.sh)" ]; then
  ok "(a) no '0.0.0.0' literal in service modules, scripts or launchers"
else
  bad "(a) '0.0.0.0' literal found in our code:"
  code_wildcard_hits "${SVC}"/*.py "${SVC}/scripts"/*.py "${REPO}/bin"/*.sh | sed 's/^/      /'
fi

# ── (b) unauthenticated mutations are rejected ────────────────────────────────
echo
echo "--- (b) mutating endpoints require auth ---"
unauth_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/speakers" \
  -H 'Content-Type: application/json' -d '{"name":"unauth-probe"}')
[ "$unauth_code" = "401" ] && ok "(b) POST /speakers without token -> 401" \
  || bad "(b) POST /speakers without token -> ${unauth_code} (expected 401)"
wrong_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/speakers" \
  -H 'Authorization: Bearer definitely-not-the-token' \
  -H 'Content-Type: application/json' -d '{"name":"wrong-token-probe"}')
[ "$wrong_code" = "401" ] && ok "(b) POST /speakers with a wrong token -> 401" \
  || bad "(b) POST /speakers with a wrong token -> ${wrong_code} (expected 401)"
del_code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "${BASE}/speakers/1")
[ "$del_code" = "401" ] && ok "(b) DELETE /speakers/1 without token -> 401" \
  || bad "(b) DELETE /speakers/1 without token -> ${del_code} (expected 401)"

# ── (c) CORS disabled ─────────────────────────────────────────────────────────
echo
echo "--- (c) CORS disabled ---"
cors_hit=0
for probe in "${BASE}/health" "${BASE}/meetings"; do
  if curl -s -D - -o /dev/null -H 'Origin: http://evil.example' "$probe" \
    | grep -qi '^access-control-allow'; then
    bad "(c) Access-Control-Allow-* header present on ${probe}"
    cors_hit=1
  fi
done
if curl -s -D - -o /dev/null -X OPTIONS "${BASE}/speakers" \
  -H 'Origin: http://evil.example' -H 'Access-Control-Request-Method: POST' \
  | grep -qi '^access-control-allow'; then
  bad "(c) Access-Control-Allow-* header present on OPTIONS preflight"
  cors_hit=1
fi
[ "$cors_hit" = 0 ] && ok "(c) no Access-Control-Allow-* header on health, /meetings or preflight"

# ── (d) data dir and DB are 0700 ──────────────────────────────────────────────
echo
echo "--- (d) data dir + DB permissions ---"
dir_mode=$(stat -f '%Lp' "${DATA_DIR}" 2>/dev/null)
db_mode=$(stat -f '%Lp' "${DB}" 2>/dev/null)
[ "$dir_mode" = "700" ] && ok "(d) data dir ${DATA_DIR} is 0700" \
  || bad "(d) data dir ${DATA_DIR} is ${dir_mode:-missing} (expected 700)"
[ "$db_mode" = "700" ] && ok "(d) DB ${DB} is 0700" \
  || bad "(d) DB ${DB} is ${db_mode:-missing} (expected 700)"

# ── (e) HF token only in the Keychain ─────────────────────────────────────────
echo
echo "--- (e) HF token only in Keychain ---"
HF_TOKEN=$(security find-generic-password -s "${KEYCHAIN_HF}" -w 2>/dev/null)
if [ -n "${HF_TOKEN}" ]; then
  ok "(e) HF token is readable from Keychain ${KEYCHAIN_HF} (len=${#HF_TOKEN})"
else
  bad "(e) HF token not found in Keychain ${KEYCHAIN_HF}"
fi
if [ -n "${HF_TOKEN}" ]; then
  if git -C "${REPO}" grep -qF -- "${HF_TOKEN}" 2>/dev/null; then
    bad "(e) exact HF token found in tracked repo files"
  else
    ok "(e) exact HF token absent from tracked repo files"
  fi
  if grep -rIF --exclude-dir=.venv --exclude-dir=.git --exclude-dir=node_modules \
    --exclude-dir=__pycache__ --exclude-dir=.cache -- "${HF_TOKEN}" "${REPO}" 2>/dev/null | grep -q .; then
    bad "(e) exact HF token found in the working tree"
  else
    ok "(e) exact HF token absent from the working tree"
  fi
  if [ -z "${VASTACK_HF_TOKEN:-}" ]; then
    ok "(e) VASTACK_HF_TOKEN is unset (no env leak)"
  else
    bad "(e) VASTACK_HF_TOKEN is set in the environment"
  fi
fi
env_files=$(find "${REPO}" -name '.env' -not -path '*/.venv/*' -not -path '*/.git/*' 2>/dev/null)
[ -z "$env_files" ] && ok "(e) no .env file exists (only .env.example)" \
  || bad "(e) unexpected .env file(s): ${env_files}"
if git -C "${REPO}" grep -qE 'hf_[A-Za-z0-9]{20,}' 2>/dev/null; then
  bad "(e) a realistic-shaped HF token literal is committed"
else
  ok "(e) no realistic-shaped hf_ token literal in tracked files"
fi
if git -C "${REPO}" grep -qF 'VASTACK_TOKEN=' -- 'speaker-service/.env.example' 2>/dev/null \
  && ! grep -qE '^VASTACK_TOKEN=.+$' "${SVC}/.env.example"; then
  ok "(e) .env.example ships no token value"
else
  bad "(e) .env.example may carry a token value"
fi

# ── (f) + (h) isolated in-process checks (switch + voiceprint purge) ──────────
echo
echo "--- (f) delete-audio switch + (h) voiceprint purge ---"
PY_OUT=$(VS_SVC="${SVC}" "${PY}" - <<'PY'
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

svc = os.environ["VS_SVC"]
sys.path.insert(0, svc)
sys.path.insert(0, os.path.join(svc, "tests"))

import conftest
from app import create_app
from config import Settings
from fastapi.testclient import TestClient
from registry import open_registry

TOKEN = "sec-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
results: list[tuple[bool, str]] = []


def record(cond: bool, msg: str) -> None:
    results.append((bool(cond), msg))


def build(data_dir: Path, delete_after: bool):
    db = data_dir / "registry.db"
    app = create_app(
        settings=Settings(
            data_dir=data_dir,
            token=None,
            delete_audio_after_transcribe=delete_after,
        ),
        registry_factory=lambda: open_registry(db),
        runner=conftest.fake_runner,
        embedder_factory=lambda: conftest.FakeEmbedder(),
        audio_loader=conftest.fake_loader,
        read_token=lambda: TOKEN,
    )
    return app, db


def upload(client, title: str):
    return client.post(
        "/meetings",
        headers=AUTH,
        files={"file": ("private.wav", b"RIFF-fake-audio", "audio/wav")},
        data={"title": title},
    )


def vp_count(db: Path, speaker_id: int) -> int:
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA query_only=ON")
        return con.execute(
            "SELECT COUNT(*) FROM voiceprints WHERE speaker_id = ?", (speaker_id,)
        ).fetchone()[0]
    finally:
        con.close()


# (f) switch ON deletes the upload; switch OFF (default) keeps it.
on_dir = Path(tempfile.mkdtemp())
app_on, _ = build(on_dir, True)
resp = upload(TestClient(app_on), "Private")
record(resp.status_code == 201, f"switch ON: upload accepted (HTTP {resp.status_code})")
audio_on = Path(resp.json()["audio_path"]) if resp.status_code == 201 else None
record(audio_on is not None and not audio_on.exists(),
       "switch ON: uploaded audio deleted after the job")
uploads_on = list((on_dir / "uploads").glob("*"))
record(not uploads_on, "switch ON: uploads dir left empty")

off_dir = Path(tempfile.mkdtemp())
app_off, _ = build(off_dir, False)
resp = upload(TestClient(app_off), "Private")
record(resp.status_code == 201, f"switch OFF: upload accepted (HTTP {resp.status_code})")
audio_off = Path(resp.json()["audio_path"]) if resp.status_code == 201 else None
record(audio_off is not None and audio_off.exists(),
       "switch OFF: uploaded audio retained")
record(Settings().delete_audio_after_transcribe is False,
       "default VASTACK_DELETE_AUDIO_AFTER_TRANSCRIBE is OFF")

# (h) DELETE /speakers/{id} purges voiceprints (sqlite count before/after).
h_dir = Path(tempfile.mkdtemp())
app_h, db_h = build(h_dir, False)
client = TestClient(app_h)
speaker = client.post("/speakers", headers=AUTH, json={"name": "Ada"}).json()
meeting = upload(client, "M").json()
cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
attached = client.post(
    f"/speakers/{speaker['id']}/voiceprints",
    headers=AUTH,
    json={"meeting_id": meeting["meeting_id"], "cluster_id": cluster_id},
)
record(attached.status_code == 201, f"voiceprint attached (HTTP {attached.status_code})")
before = vp_count(db_h, speaker["id"])
deleted = client.delete(f"/speakers/{speaker['id']}", headers=AUTH)
after = vp_count(db_h, speaker["id"])
record(before >= 1, f"voiceprints present before delete (count={before})")
record(deleted.status_code == 200, f"DELETE /speakers/{speaker['id']} -> HTTP {deleted.status_code}")
record(after == 0, f"voiceprints purged after delete (count={after})")
con = sqlite3.connect(str(db_h))
con.execute("PRAGMA query_only=ON")
gone = con.execute("SELECT COUNT(*) FROM speakers WHERE id = ?", (speaker["id"],)).fetchone()[0] == 0
con.close()
record(gone, "speaker row removed")

failed = 0
for passed, msg in results:
    print(f"{'PASS' if passed else 'FAIL'}|{msg}")
    failed += 0 if passed else 1
sys.exit(1 if failed else 0)
PY
)
PY_RC=$?
printf '%s\n' "${PY_OUT}" | sed 's/^PASS|/PASS  /; s/^FAIL|/FAIL  /'
PASS=$((PASS + $(printf '%s\n' "${PY_OUT}" | grep -c '^PASS|')))
FAIL=$((FAIL + $(printf '%s\n' "${PY_OUT}" | grep -c '^FAIL|')))
[ "$PY_RC" = 0 ] || note "(f)/(h) probe exited ${PY_RC}"

# ── (i) UI session cookie auth + token-free assets ────────────────────────────
echo
echo "--- (i) UI session cookie + token-free served assets ---"
JAR=$(mktemp)
curl -s -c "$JAR" -o /dev/null "${BASE}/"
if grep -q 'vs_session' "$JAR"; then
  ok "(i) GET / mints a vs_session cookie"
else
  bad "(i) GET / did not mint a vs_session cookie"
fi
cookie_code=$(curl -s -b "$JAR" -o /dev/null -w '%{http_code}' -X POST "${BASE}/speakers" \
  -H 'Origin: http://127.0.0.1:3910' -H 'Content-Type: application/json' \
  -d '{"name":"ui-session-probe"}')
if [ "$cookie_code" = "201" ]; then
  ok "(i) session cookie + loopback Origin can mutate (HTTP 201)"
  SVC_TOKEN=$(security find-generic-password -s "${KEYCHAIN_SVC}" -a "${KEYCHAIN_SVC_ACCT}" -w 2>/dev/null)
  PROBE_ID=$(curl -s -H "Authorization: Bearer ${SVC_TOKEN}" "${BASE}/speakers" \
    | "${PY}" -c 'import json,sys; print(next((s["id"] for s in json.load(sys.stdin)["speakers"] if s["name"]=="ui-session-probe"), ""))')
  [ -n "$PROBE_ID" ] && curl -s -o /dev/null -X DELETE -H "Authorization: Bearer ${SVC_TOKEN}" \
    "${BASE}/speakers/${PROBE_ID}"
else
  bad "(i) session cookie + loopback Origin mutation -> ${cookie_code} (expected 201)"
fi
csrf_code=$(curl -s -b "$JAR" -o /dev/null -w '%{http_code}' -X POST "${BASE}/speakers" \
  -H 'Content-Type: application/json' -d '{"name":"csrf-probe"}')
[ "$csrf_code" = "401" ] && ok "(i) cookie mutation without Origin is rejected (CSRF guard, 401)" \
  || bad "(i) cookie mutation without Origin -> ${csrf_code} (expected 401)"

SVC_TOKEN="${SVC_TOKEN:-$(security find-generic-password -s "${KEYCHAIN_SVC}" -a "${KEYCHAIN_SVC_ACCT}" -w 2>/dev/null)}"
asset_leak=0
for asset in "/" "/static/app.js" "/static/app.css"; do
  body=$(curl -s "${BASE}${asset}")
  if printf '%s' "$body" | grep -qF -- "${SVC_TOKEN}"; then
    bad "(i) service token leaked into served ${asset}"
    asset_leak=1
  fi
  if [ -n "${HF_TOKEN}" ] && printf '%s' "$body" | grep -qF -- "${HF_TOKEN}"; then
    bad "(i) HF token leaked into served ${asset}"
    asset_leak=1
  fi
done
if [ "$asset_leak" = 0 ]; then
  ok "(i) service + HF tokens appear in NO served HTML/JS/CSS asset"
fi
repo_asset_tokens=$(grep -rIF --include='*.js' --include='*.html' --include='*.css' \
  -e "${SVC_TOKEN}" ${HF_TOKEN:+-e "${HF_TOKEN}"} "${UI_STATIC}" 2>/dev/null | grep -c .)
[ "$repo_asset_tokens" = 0 ] && ok "(i) ui_static sources carry no token literal" \
  || bad "(i) ui_static sources contain a token literal"
rm -f "$JAR"

# ── NEGATIVE CONTROLS (prove the bind checks are real) ────────────────────────
echo
echo "--- negative controls (falsifiability) ---"
TMP_BAD=$(mktemp -d)
printf 'bind = "0.0.0.0"\nserver_host = "0.0.0.0"\n' > "${TMP_BAD}/config_bad.py"
if [ -n "$(code_wildcard_hits "${TMP_BAD}")" ]; then
  ok "negative-control: static scanner DETECTS a 0.0.0.0 source copy"
else
  bad "negative-control: static scanner MISSED a 0.0.0.0 source copy"
fi
BAD_PORT=$(free_port)
"${PY}" -m http.server "${BAD_PORT}" --bind 0.0.0.0 --directory "${TMP_BAD}" \
  >/dev/null 2>&1 &
BAD_PID=$!
sleep 1
if [ -n "$(wildcard_bind "${BAD_PORT}")" ]; then
  ok "negative-control: lsof probe DETECTS a 0.0.0.0 listener on :${BAD_PORT}"
else
  bad "negative-control: lsof probe MISSED a 0.0.0.0 listener on :${BAD_PORT}"
fi
kill "${BAD_PID}" 2>/dev/null || true
wait "${BAD_PID}" 2>/dev/null || true
rm -rf "${TMP_BAD}"

# ── summary ───────────────────────────────────────────────────────────────────
echo
date -u '+finished %Y-%m-%dT%H:%M:%SZ'
echo "=== summary: ${PASS} passed, ${FAIL} failed ==="
[ "${FAIL}" = 0 ] || exit 1
exit 0
