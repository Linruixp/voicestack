#!/bin/bash
# install.sh — one-shot setup for VoiceStack's speaker-service on a Mac.
#
# Automates what can be automated: installs `uv` if missing, syncs the service
# venv, mints the service token in the macOS Keychain, and prints the MCP config
# plus the remaining steps.
#
# It does NOT download models (they are gated on Hugging Face) and does NOT
# install Ollama (optional, only for local meeting summaries).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="$ROOT/speaker-service"
PY="$SERVICE/.venv/bin/python"

echo "== VoiceStack installer =="
echo "repo: $ROOT"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "WARNING: VoiceStack targets macOS on Apple Silicon; other OSes are untested." >&2
fi

# 1. uv (the Python toolchain)
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found on PATH." >&2; exit 1; }

# 2. service venv
echo "Syncing speaker-service dependencies..."
uv sync --directory "$SERVICE"

# 3. service bearer token in the Keychain (never stored in a file)
if security find-generic-password -s voicestack-service -a service -w >/dev/null 2>&1; then
  echo "Service token already present in Keychain (voicestack-service/service)."
else
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  security add-generic-password -U -s voicestack-service -a service -w "$TOKEN"
  echo "Minted service token in Keychain (voicestack-service/service)."
fi

cat <<EOF

== Service venv ready ==

1) Register the MCP server with your agent (Claude Code, Codex, Cursor, Gemini CLI, OpenCode, ...):

{
  "mcpServers": {
    "vs-speaker": {
      "command": "$PY",
      "args": ["$SERVICE/mcp_server.py"]
    }
  }
}

   OpenCode uses ~/.config/opencode/opencode.json with an "mcp" key instead:
   {"mcp":{"vs-speaker":{"type":"local","command":["$PY","$SERVICE/mcp_server.py"]}}}

   Restart your agent so it loads the new server.

2) Fetch the ASR + diarization models (one-time; needs a Hugging Face token whose
   account has accepted the gated pyannote license at
   https://huggingface.co/pyannote/speaker-diarization-community-1):

     security add-generic-password -U -s voicestack-hf -a voicestack -w <HF_TOKEN>
     HF_TOKEN="\$(security find-generic-password -s voicestack-hf -a voicestack -w)" \\
       uv run --directory "$SERVICE" --no-sync python "$SERVICE/scripts/fetch_models.py"

3) Optional - local Chinese meeting summaries (Ollama + Qwen3.5-9B, ~6.6 GB):

     brew install ollama && brew services start ollama
     ollama pull qwen3.5:9b

4) Open the Web UI:

     "$ROOT/bin/vs-web.sh"
EOF
