# VoiceStack P2d · Transcript depth (playback, inline edit, chapters) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** Make the transcript navigable and correctable — an audio player synced to the transcript, inline text correction, chapter seek (from the P2c summary), and per-speaker colors.

**Architecture:** Add two backend routes (stream meeting audio; patch one segment's text) and a client-side audio player in the transcript view. No new deps.

**Tech Stack:** Python 3.12, FastAPI/Starlette (`FileResponse` with Range), vanilla JS.

## Global Constraints
- Work dir: `/Users/LinRui/voicestack/speaker-service`; tests via `uv run pytest ...`. **Do not commit** unless approved.
- Preserve existing testids; nav stays 5.
- Speaker identity is never changed by inline **text** edits (reassignment stays in the wizard/assign view).

---

### Task 1 (backend): audio streaming + segment text patch

**Files:** `registry_meetings.py`, `api_schemas.py`, `api_operations.py`, `api_routes.py`; Test: `tests/test_api.py`, `tests/test_registry.py`

**Interfaces:**
- Produces: `MeetingOps.update_segment_text(segment_id, text) -> bool`; `GET /meetings/{id}/audio` (audio stream, Range-capable, 404 if absent); `PATCH /meetings/{id}/segments/{segment_id}` body `{text}` → updated segment.

- [ ] **Step 1: Tests** — add to `tests/test_api.py`:

```python
def test_stream_meeting_audio_and_edit_segment(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    meeting = upload_meeting(client)
    audio = client.get(f"/meetings/{meeting['meeting_id']}/audio", headers=auth)
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/")
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    segment_id = detail["segments"][0]["id"]

    patched = client.patch(
        f"/meetings/{meeting['meeting_id']}/segments/{segment_id}",
        headers=auth,
        json={"text": "更正后的文字"},
    )
    assert patched.status_code == 200
    assert patched.json()["text"] == "更正后的文字"
    after = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert after["segments"][0]["text"] == "更正后的文字"

    assert client.get("/meetings/999/audio", headers=auth).status_code == 404
    assert (
        client.patch(
            f"/meetings/{meeting['meeting_id']}/segments/999",
            headers=auth,
            json={"text": "x"},
        ).status_code
        == 404
    )
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — `registry_meetings.update_segment_text` (UPDATE segments SET text); `api_schemas.SegmentPatch(_Strict){text}`; `api_operations.update_segment` (404 if the segment isn't in the meeting, 400 on blank, returns the segment); routes:
  - `GET /meetings/{meeting_id}/audio` → 404 if `audio_path` missing/not a file, else `FileResponse(path, media_type=mimetypes.guess_type(path)[0] or "audio/mpeg")` (Starlette handles Range).
  - `PATCH /meetings/{meeting_id}/segments/{segment_id}` (`require_mutation_auth`).
- [ ] **Step 4: Run** → `uv run pytest tests/test_api.py tests/test_registry.py -q` → PASS.

---

### Task 2 (UI): audio player, sync, inline edit, chapter seek, colors

**Files:** `ui_static/index.html`, `ui_static/app.js`, `ui_static/app.css`; Test: `tests/test_ui.py`

**Interfaces:**
- Produces: `#transcript-audio` (`<audio controls>` pointing at `/meetings/{id}/audio`), testids `segment-{id}`, chapter buttons `data-seek`, and a deterministic per-speaker color class.

- [ ] **Step 1:** `index.html` `view-transcript`: add `<audio id="transcript-audio" data-testid="transcript-audio" controls preload="none"></audio>` above `#transcript-body`.
- [ ] **Step 2:** `app.js` `renderTranscript`:
  - set `#transcript-audio.src = /meetings/{id}/audio` and `hidden` when no detail.
  - render each segment as `<div class="segment" data-testid="segment-{id}" data-start="{start}" data-end="{end}">` with `esc(text)`; give `.who` a `speaker-{stableKey}` class for color.
  - click a segment → `audio.currentTime = start; audio.play()`.
  - `timeupdate` → highlight the segment whose `[start,end)` contains `audio.currentTime` (`.active`), and scroll it into view.
  - double-click segment text → inline `<textarea>`; blur/Enter → `PATCH /meetings/{id}/segments/{id}`; update `state.detail` and re-render.
  - chapters (`data-seek`) in `summaryBlock` become buttons that seek (bind in `renderIntro`).
- [ ] **Step 3:** `app.css`: `.segment.active` highlight; `#transcript-audio` full width; speaker color palette `.speaker-0..7`; chapter link buttons.
- [ ] **Step 4:** `tests/test_ui.py`: assert `transcript-audio` testid + `/audio` and `/segments/` appear in `app.js`, and `data-seek`.
- [ ] **Step 5:** Run `uv run pytest -q` + `node --check ui_static/app.js`.

---

## Self-Review
- Spec §9 transcript depth (playback sync, inline correction, chapters, speaker colors) → Tasks 1–2. Speaker **reassignment** stays in the wizard/assign view (out of scope). Long-audio Range handled by `FileResponse`.
- Testids preserved; nav unchanged.

## Execution Handoff
Inline execution; Oracle review after Task 1 and at the end (recover via `session_read` if `background_output` reports Aborted).
