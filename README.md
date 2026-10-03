# VoiceStack

**Local-first meeting transcription with speaker diarization, a durable voiceprint directory, and local Chinese summaries — driven by your AI agent over MCP.**

Everything runs on your Mac. No cloud, no uploads, no API keys. Point your agent (OpenCode, Claude Code, Codex, Cursor, Gemini CLI, …) at a recording and it returns a speaker-labelled transcript; a local Web UI lets you name the voices and play them back.

> 中文速览：本机运行的会议转写 + 说话人分离 + 声纹库 + 本地中文摘要，通过 **MCP** 接入任意智能体。一句话安装见下方 [Quick start](#quick-start-one-prompt)。全部本地运行，不上传任何音频。

---

## Why

- **Speaker-aware transcripts.** ASR (Whisper) + diarization (pyannote) + a versioned voiceprint index (WeSpeaker) → each segment is labelled with a real name once a voice is enrolled, and matched automatically in later meetings.
- **Your data stays put.** Loopback-only HTTP, a `0700` SQLite registry, secrets in the macOS Keychain. Audio never leaves the machine.
- **Agent-native.** Ships a stdio MCP server (`vs-speaker`) so any MCP-capable agent can transcribe meetings, query the directory, and drive the naming UI — no glue code.
- **Human-in-the-loop Web UI.** A local page to review the transcript, listen to a cluster's audio, and name speakers (with an explicit consent gate before storing a voiceprint).
- **Local Chinese summaries.** Optional structured summaries (TL;DR / decisions / action items / chapters) via Ollama + Qwen3.5-9B, JSON-schema-constrained.

## Components

| Path | What it is | Standalone? |
|---|---|---|
| **`speaker-service/`** | The core: meeting transcription, diarization, voiceprint directory, REST API, Web UI, local summary, and the `vs-speaker` MCP server. | ✅ Yes |
| `vsbridge/` | Thin stdio MCP proxy to **VoiceStudio**'s own MCP (TTS, voice cloning, plain transcription). | Needs VoiceStudio.app |
| `voicebridge/` | Thin stdio MCP proxy to VoiceStudio's **audiobook** + **offline translation**. | Needs VoiceStudio.app |
| `bin/` | Launchers: `install.sh`, `vs-web.sh`, `vs-speaker-up.sh`, `vs-speaker-always-on.sh`, `vs-ensure-up.sh`. | — |
| `docs/` | `USAGE.md` (full user guide), design notes, specs/plans. | — |
| `fixtures/` | Small test fixtures. | — |

**This README focuses on `speaker-service`** — the self-contained meeting/speaker stack. The two bridges are optional and require a separately installed VoiceStudio app; see [`docs/USAGE.md`](docs/USAGE.md).

## Requirements

- **macOS on Apple Silicon** (the ASR/diarization stack uses MLX + MPS).
- **[uv](https://docs.astral.sh/uv/)** (installed automatically by `bin/install.sh` if missing).
- **A Hugging Face token** whose account has accepted the gated [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) license — used once to fetch models.
- *(Optional)* **Ollama** with `qwen3.5:9b` for local summaries.
- *(Optional)* **VoiceStudio.app** for the `vsbridge` / `voicebridge` capabilities.

Model weights (Whisper large-v3, pyannote diarization, WeSpeaker ResNet34) are cached in `~/.cache/huggingface/hub` and reused across runs.

---

## Quick start (one prompt)

Paste this single line into your agent's chatbox (Claude Code, Codex, Cursor, Gemini CLI, OpenCode, …):

> **Clone `https://github.com/Linruixp/voicestack` into `~/voicestack`, run `bash ~/voicestack/bin/install.sh`, then register the `vs-speaker` MCP server from the config it prints into your MCP settings and restart so you can transcribe meeting recordings with speaker labels.**

The agent will clone the repo, run the installer (which syncs the Python env and mints the service token), and add the MCP server. After it restarts, ask it:

> "Transcribe `~/Downloads/meeting.m4a` and tell me who said what."

The **first** transcription needs the models, which are gated on Hugging Face — the installer prints the two commands to fetch them (see [Model download](#model-download)). Summaries additionally need Ollama (step 3 of the installer output).

## Manual setup

```bash
git clone https://github.com/Linruixp/voicestack ~/voicestack
bash ~/voicestack/bin/install.sh        # uv sync + Keychain token + MCP config
```

### Model download

The diarization model is gated; accept its license with your Hugging Face account first, then:

```bash
security add-generic-password -U -s voicestack-hf -a voicestack -w <HF_TOKEN>
HF_TOKEN="$(security find-generic-password -s voicestack-hf -a voicestack -w)" \
  uv run --directory ~/voicestack/speaker-service --no-sync python \
  ~/voicestack/speaker-service/scripts/fetch_models.py
```

### Optional: local summaries

```bash
brew install ollama && brew services start ollama
ollama pull qwen3.5:9b        # ~6.6 GB
```

### Run the Web UI

```bash
~/voicestack/bin/vs-web.sh     # starts the service and opens http://127.0.0.1:3910/
```

---

## Connect your agent (MCP)

`speaker-service` exposes a **stdio** MCP server. Register it once; the command is the service venv's Python plus the server script:

```
<repo>/speaker-service/.venv/bin/python   <repo>/speaker-service/mcp_server.py
```

**Claude Code / Cursor / Gemini CLI / generic `mcpServers` JSON**

```json
{
  "mcpServers": {
    "vs-speaker": {
      "command": "/Users/you/voicestack/speaker-service/.venv/bin/python",
      "args": ["/Users/you/voicestack/speaker-service/mcp_server.py"]
    }
  }
}
```

- **Claude Code**: `claude mcp add vs-speaker -- /Users/you/voicestack/speaker-service/.venv/bin/python /Users/you/voicestack/speaker-service/mcp_server.py`
- **Cursor**: `~/.cursor/mcp.json` (same `mcpServers` shape).
- **Gemini CLI**: `~/.gemini/settings.json` → `mcpServers`.
- **Codex CLI**: `~/.codex/config.toml`

  ```toml
  [mcp_servers.vs-speaker]
  command = "/Users/you/voicestack/speaker-service/.venv/bin/python"
  args = ["/Users/you/voicestack/speaker-service/mcp_server.py"]
  ```
- **OpenCode**: `~/.config/opencode/opencode.json` → `mcp` key

  ```json
  {
    "mcp": {
      "vs-speaker": {
        "type": "local",
        "command": ["/Users/you/voicestack/speaker-service/.venv/bin/python", "/Users/you/voicestack/speaker-service/mcp_server.py"]
      }
    }
  }
  ```

No token is passed to the agent: the MCP server reads the service token from the macOS Keychain, and the Web UI mints its own same-origin session cookie.

## MCP tools (`vs-speaker`)

| Tool | Purpose |
|---|---|
| `transcribe_meeting(file_path)` | Transcribe a recording; returns speaker-labelled segments, `unknown_clusters`, and a handoff `ui_url`. |
| `identify_speaker(audio_path)` | Match a clip against enrolled voiceprints → `known` / `unknown` / `re_enroll_required`. |
| `list_speakers()` | List the voiceprint directory. |
| `enroll_speaker(name, cluster_id, organization?, notes?)` | Create a speaker from a diarized cluster and enroll its voiceprint. |
| `attach_to_speaker(speaker_id, cluster_id)` | Add a cluster's voiceprint to an existing speaker. |
| `rename_speaker(speaker_id, name)` | Rename a speaker. |
| `rename_meeting(meeting_id, title)` | Edit a meeting title. |
| `get_meeting(meeting_id)` | Re-read a stored transcript. |
| `open_speaker_ui(meeting_id)` | Open the local naming UI for a meeting. |

Matching never enrolls on its own — enrollment is an explicit step. The acceptance threshold is `0.641`, calibrated on the fixture set (`VASTACK_MATCH_THRESHOLD` to override).

## Web UI

`bin/vs-web.sh` opens a single local page (`http://127.0.0.1:3910/`) with five views:

1. **Upload meeting** — pick an audio file, transcribe.
2. **Transcript** — segments with time/speaker/text, an audio player (click to seek, double-click to correct text, chapters seek), and "Resolve this cluster" buttons for unknown voices.
3. **Assign / merge / split** — name a cluster as a new speaker (consent-gated voiceprint), attach to an existing speaker, merge duplicates, or split a cluster.
4. **Speaker directory** — speakers with organization/title, consent & retention, export, and re-enroll from a past meeting.
5. **Meeting history** — search (title/summary/transcript), filters (participant/date/unresolved), date-grouped rows, editable titles, delete.

The UI is same-origin cookie-authenticated; the bearer token never reaches the browser.

## Data & privacy

| What | Where |
|---|---|
| Voiceprints, meetings, transcripts | `~/Library/Application Support/VoiceStudioStack/voicestack.db` (mode `0700`) |
| Uploaded audio (during a job) | `~/Library/Application Support/VoiceStudioStack/uploads/` |
| Model weights | `~/.cache/huggingface/hub` |
| Service token | Keychain `voicestack-service` / `service` |
| Hugging Face token | Keychain `voicestack-hf` / `voicestack` |

- Loopback only (`127.0.0.1`), no CORS, no cloud calls.
- Voiceprints are **biometric data**: enrollment is consent-gated, and each consent carries a retention term you can review/export/delete in the UI.
- The service is on-demand: it idle-exits after 10 minutes and the next tool call restarts it (`bin/vs-speaker-always-on.sh` opts into always-on).

## Development

```bash
cd speaker-service
uv sync
uv run pytest -q          # full suite (fast, offline)
```

Repo layout: `registry_*.py` (SQLite aggregates), `api_*.py` (FastAPI routes/adapters), `pipeline.py` (ASR→diarize→align→match), `enrollment.py` (speaker ops), `summarizer.py` (local LLM), `mcp_server.py` (stdio MCP), `ui_static/` (vanilla JS UI). Design specs and implementation plans live under `docs/superpowers/`.

## Troubleshooting

- **First transcription fails with a "not found" model error** → fetch the models ([Model download](#model-download)); the diarizer is pinned to MPS and raises rather than silently falling back to CPU.
- **`GatedRepoError` / 401 loading pyannote** → accept the gated license with the token's account.
- **Summary fails / times out** → ensure Ollama is running and `qwen3.5:9b` is pulled. (Thinking is disabled on purpose: Qwen3.5's chain-of-thought would blow the timeout.)
- **Service seems to keep exiting** → expected; it is on-demand. An open Web UI keeps it alive; see `docs/USAGE.md` §10.

Full guide: [`docs/USAGE.md`](docs/USAGE.md).

## License

MIT — see [LICENSE](LICENSE). Note that the diarization model you fetch is subject to **pyannote's** license, not this repo's.
