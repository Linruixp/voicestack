# VoiceStudio stack: user guide

This guide covers the local voice and speaker stack that runs on this Mac. It is
the operational companion to the design notes in `reuse-inventory.md`,
`engine-selection.md`, `privacy.md`, and `budget.md`.

Two pieces make up the stack:

- **VoiceStudio** (`VoiceStudio.app`, backend on `127.0.0.1:3900`): text to
  speech (TTS), voice cloning, generic transcription, audiobook rendering, and
  offline translation.
- **speaker-service** (`~/voicestack/speaker-service`, backend on
  `127.0.0.1:3910`): meeting transcription with speaker diarization, a speaker
  directory with voiceprints, and cross-recording identification.

OpenCode (the OMO agent) talks to both through four MCP servers. Everything runs
on loopback, uses the shared Hugging Face cache that is already on disk, and
never sends audio to a cloud service.

---

## 1. Prerequisites

- macOS on Apple silicon; FileVault on for at-rest protection of the biometric
  database (see `privacy.md`).
- VoiceStudio installed at `/Applications/VoiceStudio.app` (this host runs
  0.5.6).
- The service venv at `~/voicestack/speaker-service/.venv` and the bridge venvs
  under `~/voicestack/{vsbridge,voicebridge}` (all created by `uv`).
- Model weights cached in `~/.cache/huggingface/hub` (see section 7).
- Secrets live in the macOS Keychain, never in a file:
  - `voicestack-service` / account `service`: the speaker-service bearer token.
  - `voicestack-hf` / account `voicestack`: a Hugging Face read token (used once
    to accept the gated pyannote license and fetch models).

Ports in use: **3900** (VoiceStudio backend) and **3910** (speaker-service).

---

## 2. OpenCode / OMO MCP configuration

The four servers are already registered in `~/.config/opencode/opencode.json`
under the `mcp` key. This is the exact block that is running:

```json
{
  "mcp": {
    "mineru": {
      "type": "local",
      "command": [
        "/Users/LinRui/mineru_project/venv/bin/python",
        "/Users/LinRui/mineru_project/mcp_server/mineru_mcp_server.py"
      ],
      "environment": {
        "MINERU_CMD": "/Users/LinRui/mineru_project/venv/bin/mineru",
        "MINERU_MODEL_SOURCE": "local",
        "PYTHONPATH": "/Users/LinRui/mineru_project/venv/lib/python3.11/site-packages"
      },
      "enabled": true,
      "timeout": 600000
    },
    "vsbridge": {
      "type": "local",
      "command": [
        "/opt/homebrew/bin/uv",
        "run",
        "--directory",
        "/Users/LinRui/voicestack/vsbridge",
        "vsbridge"
      ],
      "enabled": true
    },
    "voicebridge": {
      "type": "local",
      "command": [
        "/opt/homebrew/bin/uv",
        "run",
        "--directory",
        "/Users/LinRui/voicestack/voicebridge",
        "voicebridge"
      ],
      "enabled": true
    },
    "vs-speaker": {
      "type": "local",
      "command": [
        "/Users/LinRui/voicestack/speaker-service/.venv/bin/python",
        "/Users/LinRui/voicestack/speaker-service/mcp_server.py"
      ],
      "enabled": true
    }
  }
}
```

All four servers speak stdio only. They never bind a socket, and the two
VoiceStudio bridges cold-start the backend via `~/voicestack/bin/vs-ensure-up.sh`
on the first tool call, then wait for `:3900` before forwarding.

### Tool manifest (machine checked)

Every tool below is exposed by the running servers. The names must match
`tests/test_guide.py`, which spawns each server and compares this block to the
live `tools/list`. Edit a tool name in one place only when both the code and
this block change together.

<!-- MCP-TOOL-MANIFEST (machine checked by speaker-service/tests/test_guide.py) -->
```json
{
  "mineru": [
    "mineru_parse_document",
    "mineru_batch_parse",
    "mineru_extract_images",
    "mineru_get_document_info",
    "mineru_health_check"
  ],
  "vsbridge": [
    "generate_speech",
    "list_voices",
    "list_personalities",
    "list_languages",
    "transcribe",
    "check_health",
    "clone_voice"
  ],
  "voicebridge": [
    "generate_audiobook",
    "translate_text"
  ],
  "vs-speaker": [
    "transcribe_meeting",
    "list_speakers",
    "enroll_speaker",
    "attach_to_speaker",
    "identify_speaker",
    "get_meeting",
    "rename_speaker",
    "rename_meeting",
    "open_speaker_ui"
  ]
}
```
<!-- /MCP-TOOL-MANIFEST -->

That is 23 tools in total. From OMO you rarely name a tool yourself; you write a
prompt and the agent picks the tool. The prompts and calls below show both
layers so you can predict what will run.

### Saved voices

Two clone profiles were created in task 8 and are reused everywhere:

| Name | Profile id | Language | Reference |
|------|------------|----------|-----------|
| `base_en` | `56ee2a5b` | en | `~/voicestack/fixtures/refs/ref_en.wav` |
| `base_zh` | `513da105` | zh | `~/voicestack/fixtures/refs/ref_zh.wav` |

Ask OMO to run `vsbridge.list_voices` any time to reprint the live list.

---

## 3. Capability 1: text to speech (TTS)

**OMO prompt**

> Read this aloud in English with the `base_en` voice, quality settings:
> "Welcome to the local voice stack. Everything here runs on this Mac."

**Underlying tool:** `vsbridge.generate_speech`

```json
{
  "text": "Welcome to the local voice stack.",
  "language": "en",
  "profile_id": "56ee2a5b",
  "steps": 32
}
```

Notes:

- `steps` trades speed for quality: `8` is a fast draft, `16` is balanced (the
  default), `32` is quality.
- `instruct` accepts a style hint such as `"narrator"` or `"whisper"`. Run
  `vsbridge.list_personalities` for the preset list.
- `speed` is a `0.5` to `2.0` multiplier.
- `language` accepts `"Auto"` or an ISO code; VoiceStudio ships 646 voices.
- The WAV lands under `~/Library/Application Support/OmniVoice/outputs/`. Prefer
  file output modes for long text so audio never enters the model context.

---

## 4. Capability 2: voice cloning

**OMO prompt**

> Clone the voice in `~/voicestack/fixtures/refs/ref_en.wav` as a profile named
> `guest`, then read the intro paragraph with it.

**Underlying tools:** `vsbridge.clone_voice`, then `vsbridge.generate_speech`

```json
{
  "name": "guest",
  "ref_audio_base64": "<base64 of a 10 to 20 second clean wav>",
  "ref_text": "the exact words spoken in the reference clip",
  "language": "en"
}
```

The response carries the new `profile_id`. Feed it back into
`generate_speech`:

```json
{ "text": "The intro paragraph.", "profile_id": "<returned id>", "language": "en" }
```

Notes:

- Pass the reference clip as `ref_audio_base64`. VoiceStudio refuses
  `ref_audio_path` unless `OMNIVOICE_MCP_BASE_PATH` is set and names a
  directory, so the base64 lane is the one that works out of the box.
- Use a clean 10 to 20 second sample with one speaker and no music.
- Creating a clone is a consent-bearing action. Only clone voices you have the
  right to use.
- Delete a profile you no longer need with `DELETE /profiles/<id>` against
  `http://127.0.0.1:3900`.

---

## 5. Capability 3: meeting transcription plus speaker identification and query

This is the speaker-service lane. It diarizes a recording into speaker-labelled
segments, keeps a durable speaker directory, and matches known voices across
recordings.

**OMO prompt (transcribe)**

> Transcribe `~/recordings/weekly-sync.m4a` and show me who said what.

**Underlying tool:** `vs-speaker.transcribe_meeting`

```json
{ "file_path": "/Users/LinRui/recordings/weekly-sync.m4a" }
```

The result is a meeting object with `meeting_id`, `segments` (each with `start`,
`end`, `text`, `speaker_id`, `cluster_id`), `speakers`, and
`unknown_clusters`. Cold start loads the whisper and pyannote models, so the
first call of a session can take a minute.

**OMO prompt (identify a clip)**

> Whose voice is in `~/recordings/clip.wav`?

**Underlying tool:** `vs-speaker.identify_speaker`

```json
{ "audio_path": "/Users/LinRui/recordings/clip.wav" }
```

Returns `known`, `unknown`, or `re_enroll_required`, plus the best match and its
cosine score. The acceptance threshold is `0.641`, calibrated on the fixture
set; override it only for experiments with `VASTACK_MATCH_THRESHOLD`.

**OMO prompt (query the directory and a transcript)**

> List the enrolled speakers, then fetch meeting 3 and group the segments by
> speaker.

**Underlying tools:** `vs-speaker.list_speakers`, `vs-speaker.get_meeting`

```json
{ "meeting_id": 3 }
```

Speaker directory tools:

| Tool | Purpose |
|------|---------|
| `vs-speaker.list_speakers` | List speakers with voiceprint counts. |
| `vs-speaker.enroll_speaker` | Create a NEW speaker from a diarized cluster. |
| `vs-speaker.attach_to_speaker` | Add a cluster's voiceprint to an EXISTING speaker. |
| `vs-speaker.rename_speaker` | Rename a speaker. |
| `vs-speaker.rename_meeting` | Edit a meeting's title. |
| `vs-speaker.open_speaker_ui` | Open the speaker-naming Web UI for a meeting. |
| `vs-speaker.identify_speaker` | Match a clip against enrolled voiceprints. |
| `vs-speaker.get_meeting` | Re-read a stored transcript. |
| `vs-speaker.transcribe_meeting` | Transcribe a new recording. |

`enroll_speaker` and `attach_to_speaker` take a `cluster_id`. Cluster ids are
globally unique, so the service resolves the meeting for you:

```json
{ "name": "Samantha", "cluster_id": 7, "organization": "Acme" }
```

Matching never enrolls on its own. A recognized cluster comes back labelled and
is not listed in `unknown_clusters`, so there is nothing to confirm after the
fact. Enrollment is an explicit operator step (see the Web UI flow in
section 8).

---

## 6. Capability 4: audiobook generation

**OMO prompt**

> Turn `~/docs/handbook.epub` into an English audiobook with the `base_en`
> voice and save it as m4b.

**Underlying tool:** `voicebridge.generate_audiobook`

```json
{
  "import_path": "/Users/LinRui/docs/handbook.epub",
  "voice": "56ee2a5b",
  "language": "en",
  "format": "m4b"
}
```

Notes:

- Accepted inputs: `.txt`, `.md`, `.epub`, `.pdf`.
- `format` is `m4b` or `mp3`.
- `voice` takes a saved profile id (or an empty string for the default voice).
- For a scanned PDF, parse it to Markdown first with MinerU, then feed the
  Markdown to `generate_audiobook`. See section 7 for the MinerU tools.
- Rendering is the long pole: a short chapter set takes about 80 to 105 seconds
  on this host when the model is warm. The bridge allows up to 1800 seconds per
  render.

**MinerU (document parsing, for scanned PDFs)**

| Tool | Purpose |
|------|---------|
| `mineru.mineru_parse_document` | PDF to Markdown or JSON. |
| `mineru.mineru_batch_parse` | Parse every PDF in a directory. |
| `mineru.mineru_extract_images` | Pull embedded images out. |
| `mineru.mineru_get_document_info` | Read PDF metadata. |
| `mineru.mineru_health_check` | Confirm the CLI and models are usable. |

Note the real tool names carry the `mineru_` prefix. Scanned OCR needs a local
model config (see troubleshooting), otherwise the parser silently falls back to
a text extractor that drops the page images.

---

## 7. Capability 5: translation and generic transcription

**OMO prompt (translate)**

> Translate this paragraph from English to Chinese, offline.

**Underlying tool:** `voicebridge.translate_text`

```json
{
  "text": "Local first means the audio never leaves the machine.",
  "src": "en",
  "tgt": "zh"
}
```

The bridge prefers the offline Argos engine and installs the language pack on
first use. The very first Argos call after an idle period can fail with a
dropped connection while the pack cold-loads; retry once and it succeeds in
under a second. It falls back to any ready provider if Argos is unavailable.

**OMO prompt (transcribe without speaker labels)**

> Transcribe `~/recordings/interview.mp3` to plain text.

**Underlying tool:** `vsbridge.transcribe`

```json
{ "audio_path": "interview.mp3", "language": "auto" }
```

Notes:

- Pass exactly one of `audio_path` or `audio_base64`.
- `audio_path` must resolve inside `OMNIVOICE_MCP_BASE_PATH`. When that variable
  is unset, VoiceStudio refuses the path, so use `audio_base64` instead.
- Use `vs-speaker.transcribe_meeting` when you need speaker labels; use
  `vsbridge.transcribe` when you only need text and want the faster path.
- `vsbridge.check_health` tells the agent whether the backend is answering.

---

## 8. Web UI

The speaker-service ships a single local page with five views. It has no login
screen, no client-side router, and no business logic: each action calls an
existing API route. `GET /` mints a same-origin `HttpOnly` session cookie, so
the service bearer token never reaches the browser.

### Open it

```bash
~/voicestack/bin/vs-web.sh
```

The script starts the service if it is down (`vs-speaker-up.sh`), waits for
`GET /health`, then opens `http://127.0.0.1:3910/` in your browser.

For a headless or scripted run, suppress the browser:

```bash
VS_WEB_NO_OPEN=1 ~/voicestack/bin/vs-web.sh
```

Override the port with `VS_SPEAKER_PORT` and the health wait with
`VS_WEB_TIMEOUT` (default 60 seconds).

### The five screens

1. **Upload meeting.** Pick an audio file (wav, mp3, m4a, flac) and an optional
   title, then "Upload and transcribe". Transcription is synchronous, so a file
   can take 20 to 60 seconds.
2. **Transcript.** Choose a meeting. Segments show time, speaker label or
   `unknown`, and text. Each unknown cluster is flagged with a
   "Resolve this cluster" button that selects it for screen 3.
3. **Assign / merge / split.** With a cluster selected:
   - "Name this cluster as a NEW speaker": create a speaker and enroll its
     voiceprint in one atomic step.
   - "Assign this cluster to an EXISTING speaker": add a voiceprint to a
     speaker that already exists (no duplicate).
   - "Merge two speakers": move all voiceprints and links from a source into a
     target; the source is deleted.
   - "Split a cluster": cut one cluster into two at a chosen second when the
     diarizer merged two people.
4. **Speaker directory.** Every speaker with organization, notes, and
   voiceprint count. The delete action opens a dialog that requires you to type
   the speaker name; it warns how many biometric voiceprints will be purged.
5. **Meeting history.** Every meeting with its date and the speakers linked to
   it.

### Enroll and identify flow (the normal loop)

1. Upload a recording. New voices appear as unknown clusters.
2. On the transcript, pick an unknown cluster and resolve it. Create a new
   speaker for someone new, or attach the cluster to an existing speaker if the
   system failed to match them.
3. The cluster, its segments, and the meeting link are relabelled immediately.
4. Upload the next recording. Voices you enrolled are matched automatically
   (threshold `0.641`), so their clusters come back already labelled and are not
   flagged for confirmation.
5. Re-check the directory and history any time; nothing is deleted except an
   explicit speaker purge.

The header dot polls `GET /health` every 30 seconds. An open page therefore
counts as activity and keeps the on-demand service alive while you work.

---

## 9. Where data lives

| What | Location |
|------|----------|
| Model weights (shared, reused) | `~/.cache/huggingface/hub` |
| Service database and uploads | `~/Library/Application Support/VoiceStudioStack/` (mode `0700`) |
| Durable registry | `~/Library/Application Support/VoiceStudioStack/voicestack.db` |
| Meeting uploads during a job | `~/Library/Application Support/VoiceStudioStack/uploads/` |
| Saved voice profiles (wav) | `~/Library/Application Support/OmniVoice/voices/` |
| Generated speech and audiobooks | `~/Library/Application Support/OmniVoice/outputs/` |
| VoiceStudio runtime | `~/Library/Application Support/VoiceStudio/runtime` |
| Service log | `~/voicestack/speaker-service/.uvicorn.log` |
| Service token | Keychain `voicestack-service` / `service` |
| Hugging Face token | Keychain `voicestack-hf` / `voicestack` |
| Fixtures (reference voices, meetings) | `~/voicestack/fixtures/` |

The service sets `HF_HOME` and `HF_HUB_CACHE` to the shared cache before any
model import, so nothing is downloaded twice. The registry is protected at rest
by FileVault; the service does no encryption of its own.

---

## 10. Troubleshooting

### A model is missing

Symptom: the first transcription or identification fails with a "not found" or
snapshot error, or diarization raises instead of falling back.

The service expects three cached repos: `mlx-community/whisper-large-v3-mlx`,
`pyannote/speaker-diarization-community-1`, and
`pyannote/wespeaker-voxceleb-resnet34-LM`. Re-fetch only what is missing (the
script reuses anything already cached):

```bash
HF_TOKEN="$(security find-generic-password -s voicestack-hf -a voicestack -w)" \
  uv run --directory ~/voicestack/speaker-service --no-sync python \
  scripts/fetch_models.py
```

The diarizer is pinned to the MPS device. If MPS is unavailable it raises on
purpose; there is no silent CPU fallback.

### Hugging Face license or gated model

Symptom: a 401 or 403, `GatedRepoError`, or `HFValidationError` when loading
`pyannote/speaker-diarization-community-1`.

That repo is gated. Log in to Hugging Face with the token's account, open the
model page, and accept the license. Verify the token still works:

```bash
security find-generic-password -s voicestack-hf -a voicestack -w >/dev/null && echo token-present
```

The token value must stay in the Keychain and must never be written into this
repository or any `.env` file.

### A port is busy

Symptom: the backend or the service fails to bind, or a health probe answers
from an unexpected process.

Check who owns each port:

```bash
lsof -nP -iTCP:3900 -sTCP:LISTEN   # VoiceStudio backend
lsof -nP -iTCP:3910 -sTCP:LISTEN   # speaker-service
```

If you need to move the speaker-service, set `VASTACK_PORT` (service) and
`VS_SPEAKER_PORT` (launcher and Web UI) to the same value. For VoiceStudio set
`VS_PORT` and point the bridges at it with `VS_BASE_URL`. Never bind to a
non-loopback address.

### The service keeps exiting

Symptom: an open UI or a long session drops, and the next tool call is slow.

This is expected. The service is on demand: it exits after `VASTACK_IDLE_EXIT_S`
seconds (default 600) with no in-flight request, no queued or running job, and no
`/health` keepalive. The Web UI polls `/health` every 30 seconds, so an open page
keeps it alive. After an exit, the next MCP call restarts it through
`vs-speaker-up.sh` and waits for `/health`, which is why the first call is
slower.

Do not poll `/health` yourself to test whether the process exited: the poll
itself refreshes the keepalive. Use
`lsof -nP -iTCP:3910 -sTCP:LISTEN` instead.

### Always-on (opt in)

If you want `:3910` resident so the first call is never cold, install the
launchd agent:

```bash
~/voicestack/bin/vs-speaker-always-on.sh install
~/voicestack/bin/vs-speaker-always-on.sh status
~/voicestack/bin/vs-speaker-always-on.sh uninstall
```

This is never installed by default. The agent pins `VASTACK_IDLE_EXIT_S=0` so
the idle watchdog does not fight `KeepAlive`.

### Scanned PDFs lose their content

Symptom: MinerU returns only a few dozen bytes with a `## Page 1` heading.

The stock `~/mineru.json` points the pipeline model dir at an empty ModelScope
path, so the parser falls back to a text extractor. Point
`MINERU_TOOLS_CONFIG_JSON` at a config whose `models-dir.pipeline` is the cached
Hugging Face snapshot
(`~/.cache/huggingface/hub/models--opendatalab--PDF-Extract-Kit-1.0`). The
acceptance harness in `speaker-service/tests/acceptance.sh` builds this config;
copy its pattern.

### First translate call after idle fails

Symptom: `translate_text` reports a dropped connection, then succeeds on retry.

The Argos language pack cold-loads on the first call. Retry once. If it still
fails, another provider may be offline; check the VoiceStudio provider settings.

---

## 11. Verifying this guide

The names in this guide are pinned to the running setup by a doctest-style
check. It spawns each registered MCP server over stdio, reads the live
`tools/list`, and compares it to the manifest in section 2. It also confirms the
four config keys exist in `opencode.json` and that no secret appears in this
file.

```bash
cd ~/voicestack/speaker-service && .venv/bin/python -m pytest tests/test_guide.py -q
```

If a tool is renamed in the code but not in this guide (or the reverse), the
check fails with the offending name, which is the signal to update both.
