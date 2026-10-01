# voicebridge

Thin **stdio** MCP server that exposes VoiceStudio's **audiobook** and **translation**
capabilities to OMO/OpenCode as two tools:

| Tool | Backs onto |
|---|---|
| `generate_audiobook(import_path, voice, language, format)` | `POST /audiobook/import` then `POST /audiobook` (SSE) |
| `translate_text(text, src, tgt)` | `POST /dub/translate` (Argos offline / ready provider) |

VoiceStudio is **on-demand**: before every proxy call voicebridge runs
`~/voicestack/bin/vs-ensure-up.sh`, which starts the app if `:3900` is down and
waits for the discovery document. This bridge owns **audiobook + translation**;
TTS/clone/transcribe stay on VoiceStudio's own MCP (task 37 owns that proxy).

It speaks **stdio only** — never binds a socket and never opens a database.

## Run

```bash
uv run voicebridge          # stdio MCP server (client-spawned)
```

## Environment overrides

| Var | Default | Meaning |
|---|---|---|
| `VS_BASE_URL` | `http://127.0.0.1:3900` | VoiceStudio backend base URL; its port is passed to ensure-up as `VS_PORT` |
| `VS_ENSURE_UP` | `~/voicestack/bin/vs-ensure-up.sh` | launcher invoked before each proxy call |
| `VS_ENSURE_TIMEOUT` | `120` | seconds allowed for the launcher subprocess |
| `VS_IMPORT_TIMEOUT` | `120` | seconds for `/audiobook/import` |
| `VS_RENDER_TIMEOUT` | `1800` | seconds to keep the `/audiobook` SSE stream open |
| `VS_TRANSLATE_TIMEOUT` | `180` | seconds for `/dub/translate` |
| `VS_OUTPUTS_DIR` | `~/Library/Application Support/OmniVoice/outputs` | where `done.output` basenames live |
| `VS_TRANSLATE_PROVIDER` | *(auto)* | force a provider (`argos`, `openai`, ...); default tries offline Argos first |
| `VS_ENSURE_UP_ENABLED` | `1` | set `0` to skip cold-start (tests / already-up setups) |

## Tools

Both tools always return a JSON object. Success is `{"ok": true, ...}`; failure is
`{"ok": false, "error": {"code", "message", "details"}}` — a **structured error,
never a hang**. Timeouts, a dead port, a failed launcher and a missing Argos pack
all surface this way.

`generate_audiobook` returns the absolute path to the rendered file plus an
`ffprobe` summary (format, duration, chapters, size).
