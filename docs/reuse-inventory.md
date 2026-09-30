# Reuse Inventory (todo 34)

Date: 2026-09-30
Host: macOS 26.6.2 (build 25G83), arm64, 24 GB RAM (25769803776 bytes)
Purpose: before installing anything for the voicestudio-omo-local-stack, catalogue what is already on this Mac and decide reuse vs install per component.

## Disk budget note (for tasks 4 and onward)

```
$ df -h /System/Volumes/Data
Filesystem      Size    Used   Avail Capacity iused ifree %iused  Mounted on
/dev/disk3s5   460Gi   385Gi    29Gi    93%    2.6M  307M    1%   /System/Volumes/Data
```

Only ~29 GiB free on the data volume (93 percent used). The plan's task-4 gate is "free disk >= 40 GB before downloads", so this host currently FAILS that gate. Any download-heavy task must first reclaim space or get a user decision. Nothing here was downloaded or installed by this task.

## Decision table

| Component | Location | Present? | Decision | Notes |
|-----------|----------|----------|----------|-------|
| MinerU MCP server | `~/mineru_project/` (venv `venv/bin/python`, 3.11.14; server `mcp_server/mineru_mcp_server.py`) | YES | **REUSE** | Already registered in `~/.config/opencode/opencode.json` under `mcp.mineru`, `enabled: true`. Do NOT re-register or duplicate the entry. MinerU 3.1.0 + mcp_mineru 0.1.3 in its venv (2.0 GB). |
| mlx-whisper venv (audio-transcriber skill) | `~/.cache/mlx-whisper/venv` | YES | **REUSE** | Python 3.11.14 venv, `mlx_whisper 0.4.3`, `mlx 0.31.2`, torch 2.12.0; import check passes. 895 MB. This is the audio-transcriber skill venv. The speaker service must NOT install into it (plan header rule); the service builds its OWN venv and reuses only cached model weights. |
| Cached HF model weights | `~/.cache/huggingface/hub` | YES | **REUSE** | 5 `models--*` dirs, 2.7 GB total. Reuse in place so no model is re-downloaded; set `HF_HOME`/cache to this hub. |
| VoiceStudio application | `/Applications/VoiceStudio.app` | NO | **INSTALL** | Official macOS aarch64 package, version-pinned (plan task 6). Not present on this host. |
| Python (3.11 / 3.12) | `/opt/homebrew/bin/python3.11`, `/opt/homebrew/bin/python3.12` | YES | **REUSE** | 3.11.14 and 3.12.12 present. System default `python3` is 3.14.2 (NOT in the plan's allowed 3.11/3.12 set; use 3.11 for compatibility). |
| uv | `/opt/homebrew/bin/uv` | YES | **REUSE** | uv 0.10.0 (Homebrew 2026-02-05). |
| ffmpeg | `/opt/homebrew/bin/ffmpeg` | YES | **REUSE** | ffmpeg 8.1.1. Required by the speaker service. |
| git | `/usr/bin/git` | YES | **REUSE** | git 2.50.1 (Apple Git-155). |
| sox | (none) | NO | **INSTALL** | `sox` NOT found on PATH. Only audio tooling gap; install via Homebrew if the service needs it. |
| pyannote speaker-diarization-community-1 | HF cache | NO | **INSTALL (download)** | No `models--pyannote*` in the hub. Gated; needs accepted license (plan task 3). |
| WeSpeaker ResNet34 embedder (`pyannote/wespeaker-voxceleb-resnet34-LM`) | HF cache | NO | **INSTALL (download)** | No `models--wespeaker*` / `models--pyannote*--wespeaker*`. Canonical 256-d identity embedder (plan header). |
| mlx-whisper large-v3 weights | HF cache | NO | **INSTALL (download)** | Cache has `whisper-base-mlx` and `whisper-large-v3-turbo` only; large-v3 is NOT cached. Service default per plan task 15. |
| sentence-transformers (MiniLM) models | HF cache | YES | **REUSE if needed** | `all-MiniLM-L6-v2` (32 KB, weights not fully pulled) and `multi-qa-MiniLM-L6-cos-v1` (87 MB). Not named in the plan; reuse only if a component actually calls for them. |
| Translation engine (Argos/NLLB) | n/a | NO | **INSTALL if used** | Referenced by VoiceStudio `docs/calls.md`; not inventoried as a local asset. Confirm during the translate task. |

## Verbatim HuggingFace hub listing

```
$ ls -la ~/.cache/huggingface/hub
total 16
drwxr-xr-x@ 10 LinRui  staff  320 Jun  9 08:53 .
drwxr-xr-x@  5 LinRui  staff  160 Sep 29 20:42 ..
drwxr-xr-x@  8 LinRui  staff  256 Jun  8 23:51 .locks
-rw-r--r--@  1 LinRui  staff  191 May 12 19:48 CACHEDIR.TAG
drwxr-xr-x@  5 LinRui  staff  160 May 14 22:58 models--mlx-community--whisper-base-mlx
drwxr-xr-x@  5 LinRui  staff  160 May 12 19:48 models--mlx-community--whisper-large-v3-turbo
drwxr-xr-x@  5 LinRui  staff  160 Sep 29 20:42 models--opendatalab--PDF-Extract-Kit-1.0
drwxr-xr-x@  6 LinRui  staff  192 Jun  8 23:39 models--sentence-transformers--all-MiniLM-L6-v2
drwxr-xr-x@  6 LinRui  staff  192 Jun  8 23:53 models--sentence-transformers--multi-qa-MiniLM-L6-cos-v1
-rw-r--r--@  1 LinRui  staff    1 Aug  4  2025 version.txt

$ du -sh ~/.cache/huggingface/hub/models--*
137M	/Users/LinRui/.cache/huggingface/hub/models--mlx-community--whisper-base-mlx
 85M	/Users/LinRui/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo
2.4G	/Users/LinRui/.cache/huggingface/hub/models--opendatalab--PDF-Extract-Kit-1.0
 32K	/Users/LinRui/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2
 87M	/Users/LinRui/.cache/huggingface/hub/models--sentence-transformers--multi-qa-MiniLM-L6-cos-v1
2.7G	/Users/LinRui/.cache/huggingface/hub   (total)
```

## Verbatim MinerU MCP entry (from `~/.config/opencode/opencode.json`)

```json
{
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
  }
}
```

This entry is reused as-is. Do NOT duplicate or modify it.

## Toolchain versions

```
$ which uv ffmpeg git python3 sox
/opt/homebrew/bin/uv
/opt/homebrew/bin/ffmpeg
/usr/bin/git
/opt/homebrew/bin/python3
sox not found

$ uv --version        -> uv 0.10.0 (Homebrew 2026-02-05)
$ ffmpeg -version     -> ffmpeg version 8.1.1 Copyright (c) 2000-2026 the FFmpeg developers
$ git --version       -> git version 2.50.1 (Apple Git-155)
$ python3 --version   -> Python 3.14.2
$ python3.11 --version -> Python 3.11.14
$ python3.12 --version -> Python 3.12.12
```

## Bottom line

- REUSE: MinerU MCP server + its registration; the mlx-whisper venv (do not install into it); cached HF models (`PDF-Extract-Kit-1.0`, whisper-base, whisper-large-v3-turbo, MiniLM pair); Python 3.11/3.12, uv, ffmpeg, git.
- INSTALL: VoiceStudio.app itself; `sox`; plus the not-yet-cached models (mlx-whisper large-v3, pyannote community-1, WeSpeaker ResNet34) and any translation engine, all subject to the disk gate.
- No component was installed, downloaded, or modified by this task. Inventory only.
