# vsbridge

Thin **stdio** MCP server that proxies VoiceStudio's **own MCP tools** to
OMO/OpenCode, cold-starting VoiceStudio on demand.

VoiceStudio is on-demand: before every tool call vsbridge runs
`~/voicestack/bin/vs-ensure-up.sh`, which starts `/Applications/VoiceStudio.app`
if `:3900` is down and waits for its discovery document. The call is then
forwarded to VoiceStudio's Streamable-HTTP MCP at `http://127.0.0.1:3900/mcp/`
and its result is returned verbatim.

It **is** a proxy — it does not reimplement any tool. The tool surface is
whatever the installed VoiceStudio actually returns from `tools/list` (v0.5.6
ships **7**: `generate_speech`, `list_voices`, `list_personalities`,
`list_languages`, `transcribe`, `check_health`, `clone_voice`).

It owns **TTS / voice cloning / transcription only**. Audiobook generation and
translation belong to the sibling `voicebridge` (task 13), which ensure-ups
independently. vsbridge does **not** proxy audiobook/translate.

It speaks **stdio only** — never binds a socket.

## Run

```bash
uv run vsbridge          # stdio MCP server (client-spawned)
```

## Environment overrides

| Var | Default | Meaning |
|---|---|---|
| `VS_BASE_URL` | `http://127.0.0.1:3900` | VoiceStudio backend base URL; its port is passed to ensure-up as `VS_PORT` |
| `VS_MCP_PATH` | `/mcp/` | VoiceStudio MCP endpoint path |
| `VS_ENSURE_UP` | `~/voicestack/bin/vs-ensure-up.sh` | launcher invoked before each tool call |
| `VS_ENSURE_UP_ENABLED` | `1` | set `0` to skip the launcher (tests / already-up setups) |
| `VS_ENSURE_TIMEOUT` | `120` | seconds allowed for the launcher subprocess |
| `VS_HEALTH_TIMEOUT` | `20` | seconds to confirm `/health` after the launcher |
| `VS_MCP_TIMEOUT` | `300` | seconds per MCP request (raise for long clones) |
| `VS_PROBE_TIMEOUT` | `0.5` | seconds for the `tools/list` reachability probe |
| `VS_CATALOG` | `~/voicestack/vsbridge/vsbridge/catalog.json` | cached live tool catalog |

## How `tools/list` and `tools/call` behave

* **`tools/call`** — always runs `vs-ensure-up.sh` (unless disabled), waits for
  `/health`, then forwards the call. So calling any tool with VoiceStudio
  **closed cold-starts the app**; with it **already up** the launcher's fast
  path returns in milliseconds and no relaunch occurs.
* **`tools/list`** — never cold-starts. If VoiceStudio is already serving, the
  live `tools/list` is forwarded and cached; if it is down, the cached
  live-derived catalog (shipped as `vsbridge/catalog.json`) answers. The tool
  surface therefore always mirrors a real VoiceStudio `tools/list` result.

Every failure (launcher missing/failed, `/health` timeout, MCP unreachable,
JSON-RPC error, unexpected exception) is returned as a structured payload:

```json
{"ok": false, "error": {"code": "...", "message": "...", "details": {}}}
```

— never a hang.

## Register in OpenCode/OMO

> Task 28 owns registration in `~/.config/opencode/opencode.json`. **Do not add
> this entry by hand unless you are task 28.** Appending (never replacing) is
> required; the existing `mineru` entry must be preserved.

```jsonc
{
  "mcp": {
    "vsbridge": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/Users/LinRui/voicestack/vsbridge", "vsbridge"],
      "enabled": true
    }
  }
}
```

If `uv` is not on the GUI PATH, use the resolved interpreter instead:

```jsonc
{
  "mcp": {
    "vsbridge": {
      "type": "local",
      "command": ["/Users/LinRui/voicestack/vsbridge/.venv/bin/vsbridge"],
      "enabled": true
    }
  }
}
```

## Tests

```bash
uv run pytest                 # fast, offline (structured errors + catalog fallback)
VS_BRIDGE_LIVE=1 uv run pytest tests/test_live_bridge.py   # real cold-start (quits VoiceStudio)
```

The live test writes `~/.omo/evidence/voicestudio-omo-local-stack/task-37-bridge.json`.
