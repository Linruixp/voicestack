# VoiceStack P1 · Web UI (wizard + intro header) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. UI work SHOULD be executed by a `visual-engineering` agent with the `frontend` skill.

**Goal:** In the existing vanilla five-view UI, add (a) a speaker-naming **wizard** reachable from the agent's deep link that plays a per-cluster audio sample and resolves the batch, and (b) a **meeting intro header** (editable title + time/duration/location/participants) above the transcript, plus a speaker job-title field.

**Architecture:** No framework, no build. Extend `ui_static/index.html`, `app.js`, `app.css`. The wizard is a sixth `<section class="view">` that is **not** a nav tab (entered via `?meeting=&task=speakers&batch=` or the existing "Resolve this cluster" buttons). All calls go through the existing same-origin `api()` helper.

**Tech Stack:** Vanilla ES2020 in a single `app.js`; native `<audio>`; Playwright for e2e.

## Global Constraints

- Work dir: `/Users/LinRui/voicestack/speaker-service`. Tests: `uv run pytest ...`; e2e via `tests/e2e/run-e2e.sh`.
- **Do not commit** unless the user explicitly approves.
- **Do not add a nav button** for the wizard — `tests/e2e/ui_e2e.mjs` asserts `nav button` count `=== 5`. The wizard is a view, not a tab.
- Keep the vanilla, no-build, same-origin-cookie architecture. No new runtime deps; no `Authorization` header (use `credentials: "same-origin"`, as `api()` already does).
- Never place the service token or user PII in the URL beyond the opaque `meeting`/`batch` ids.
- Voiceprint enrollment is an explicit consent gate: "remember" defaults **unchecked**; unchecking must send `remember:false` (label-only, no biometric stored).
- Preserve all existing `data-testid`s used by `ui_e2e.mjs`.

## Backend contracts the UI consumes (already implemented)

- `GET /meetings/{id}` → `{ meeting:{id,title,date,duration_s,location,topic,original_title,...}, segments:[{id,start,end,text,speaker_id,speaker_name,cluster_id}], speakers:[name], unknown_clusters:[{cluster_id,label,segment_count,start,end,speaker_id}], links:[...], handoff:{needed,batch_id,unknown_count,threshold}, ui_url }`
- `PATCH /meetings/{id}` body `{title?,topic?,location?}` → meeting payload
- `GET /speaker-batches/{id}` → `{ batch:{id,meeting_id,state,...}, items:[{cluster_id,label,segment_count,start,end,suggested_speaker_id,similarity,resolution,sample_url}] }`
- `POST /speaker-batches/{id}/resolve` body `{items:[{cluster_id,action:"enroll"|"attach"|"skip",name?,organization?,title?,speaker_id?,remember?}]}` → `{resolved,remaining,batch:{id,meeting_id,state}}`
- `GET /clusters/{id}/sample` → `audio/wav`
- `GET /speakers` → speakers now include `title`
- Deep link: `http://127.0.0.1:3910/?meeting={id}&task=speakers&batch={id}`

---

### Task 1: Meeting intro header + editable title + speaker job title

**Files:** `ui_static/index.html`, `ui_static/app.js`, `ui_static/app.css`; Test: `tests/test_ui.py`

**Deliverable:** In `view-transcript`, above the segments, render an intro header: an inline-editable title, a meta line (date · duration · location · participants), and a summary placeholder. Add a `职务` field to the enroll form and a `职务` column + edit field to the speaker directory.

- [ ] **Step 1:** In `index.html` `view-transcript`, before `#transcript-body`, add:

```html
<div id="transcript-intro" data-testid="transcript-intro" hidden>
  <h3 id="transcript-title" data-testid="transcript-title" class="editable-title" title="Click to rename"></h3>
  <p id="transcript-meta" data-testid="transcript-meta" class="meta"></p>
  <p id="transcript-summary" data-testid="transcript-summary" class="summary">摘要尚未生成</p>
</div>
```

- [ ] **Step 2:** Add `职务` to the enroll form (`data-testid="enroll-title"`) and a `<th>职务</th>` + row cell (`data-testid="speaker-title-{id}"`) in `view-speakers`; add `data-field="title"` to the speaker inline-edit row.

- [ ] **Step 3:** In `app.js`:
  - `renderTranscript()`: populate `#transcript-intro` from `state.detail.meeting` (title, `date`, `duration_s` formatted via `fmt()`, `location`, `state.detail.speakers`); hide when no detail.
  - Make `#transcript-title` inline-editable: on click, swap to an `<input>`; on blur/Enter, `PATCH /meetings/{id}` `{title}`; update `state.detail.meeting.title` and re-render; show a status line.
  - `enroll-form` submit: include `title: $("enroll-title").value || null`.
  - `renderSpeakers()`/`editSpeaker()`: render and PATCH `title`.
- [ ] **Step 4:** Add minimal CSS for `.editable-title` (cursor:pointer; hover underline) and `.meta`/`.summary` muted text.
- [ ] **Step 5:** Test — add to `tests/test_ui.py`:

```python
def test_ui_serves_intro_header_and_title_fields(client: TestClient) -> None:
    html = client.get("/").text
    assert 'data-testid="transcript-intro"' in html
    assert 'data-testid="transcript-title"' in html
    assert 'data-testid="enroll-title"' in html
```

- [ ] **Step 6:** Run `uv run pytest tests/test_ui.py -q` → PASS.

---

### Task 2: Wizard view shell + deep link

**Files:** `ui_static/index.html`, `ui_static/app.js`, `ui_static/app.css`; Test: `tests/test_ui.py`

**Deliverable:** A `view-wizard` section (no nav tab) that `init()` opens when the URL has `task=speakers`, loading the batch by the `batch` (or resolving it from `meeting`).

- [ ] **Step 1:** Add the section in `index.html` (after `view-meetings`):

```html
<section id="view-wizard" class="view" data-testid="view-wizard" hidden>
  <h2>说话人命名</h2>
  <div class="wizard-head">
    <span id="wizard-progress" data-testid="wizard-progress"></span>
    <button type="button" id="wizard-exit" data-testid="wizard-exit" class="secondary">稍后继续</button>
  </div>
  <div id="wizard-card" data-testid="wizard-card"></div>
  <p id="wizard-status" data-testid="wizard-status" class="status"></p>
</section>
```

- [ ] **Step 2:** In `app.js` `init()`: parse `new URLSearchParams(location.search)`; if `task === "speakers"`: read `batch` (or, if absent, load `GET /meetings/{meeting}` and use `handoff.batch_id`); `await openWizard(batchId)`; `show("wizard")`. Add `let state.wizard = null`.
- [ ] **Step 3:** `async function openWizard(batchId)`: `state.wizard = await api("/speaker-batches/" + batchId)`; `renderWizard()`; `show("wizard")`.
- [ ] **Step 4:** `#wizard-exit` click → `show("transcript")` (or `show("meetings")` if no detail) without submitting.
- [ ] **Step 5:** Test — add to `tests/test_ui.py` that `view-wizard` is present and `test_ui.py` still passes:

```python
def test_ui_has_wizard_view_without_extra_nav_tab(client: TestClient) -> None:
    html = client.get("/").text
    assert 'data-testid="view-wizard"' in html
    assert html.count("data-view=") == 5  # wizard is NOT a nav tab
```

- [ ] **Step 6:** Run `uv run pytest tests/test_ui.py -q` → PASS.

---

### Task 3: Wizard interaction (audio playback + decisions + resolve)

**Files:** `ui_static/app.js`, `ui_static/app.css`; Test: `tests/e2e/ui_e2e.mjs`

**Deliverable:** One cluster card at a time with: label/duration/segment count, a native `<audio controls>` pointed at the cluster's `sample_url`, a claim choice (attach-existing / create-new name+org+title / skip), a consent-gated "记住声纹" checkbox (default unchecked), and progress + submit.

- [ ] **Step 1:** `renderWizard()` builds the current card for `state.wizard.items[state.wizardIndex]`:

```html
<div class="wizard-card-inner">
  <div class="wizard-cluster" data-testid="wizard-cluster-label"></div>
  <audio data-testid="wizard-sample" controls preload="none" src="/clusters/{id}/sample"></audio>
  <label><input type="radio" name="wizard-action" value="attach" data-testid="wizard-action-attach"> 关联已有说话人</label>
  <select data-testid="wizard-attach-select"></select>
  <label><input type="radio" name="wizard-action" value="enroll" data-testid="wizard-action-enroll"> 新建说话人</label>
  <input data-testid="wizard-new-name" placeholder="姓名">
  <input data-testid="wizard-new-org" placeholder="单位">
  <input data-testid="wizard-new-title" placeholder="职务">
  <label><input type="radio" name="wizard-action" value="skip" data-testid="wizard-action-skip"> 跳过</label>
  <label class="remember"><input type="checkbox" data-testid="wizard-remember"> 同时记住此声纹用于以后会议</label>
  <p class="consent">勾选后将保存声纹（生物特征数据）用于以后会议识别；默认保存 12 个月，可在声文库删除或导出。未勾选仅用于本次转写。</p>
  <div class="wizard-nav">
    <button type="button" data-testid="wizard-prev" class="secondary">上一步</button>
    <button type="button" data-testid="wizard-next">下一步</button>
    <button type="button" data-testid="wizard-submit" hidden>完成并继续</button>
  </div>
</div>
```

  - The attach `<select>` is populated from `state.speakers` (searchable via a datalist or a text filter is acceptable; a `<select>` is the minimum). Pre-select `items[i].suggested_speaker_id` when present.
  - Show `wizard-next` except on the last item, where `wizard-submit` shows.
  - `#wizard-progress` shows `第 {i+1}/{n} 个`.
- [ ] **Step 2:** Keep a `state.wizardDecisions: Record<clusterId, {action, name?, organization?, title?, speaker_id?, remember}>` updated as the user edits; `prev`/`next` move `state.wizardIndex`. Navigation must not lose entered values.
- [ ] **Step 3:** `wizard-submit` builds `items` for **every** wizard item (defaulting untouched ones to `{action:"skip"}`), then `POST /speaker-batches/{batchId}/resolve`. On success: refresh speakers/meetings, `await openMeeting(batch.meeting_id)`, `show("transcript")`, and surface `resolved`/`remaining` in `#wizard-status`. If the response `remaining > 0`, keep the wizard open and reload the batch.
- [ ] **Step 4:** Guard submit: an `enroll` decision requires a non-empty name (else focus it and show an error in `#wizard-status`).
- [ ] **Step 5:** CSS: card layout, audio full width, consent text muted/small, remember row distinct, radio groups aligned.
- [ ] **Step 6:** e2e — extend `tests/e2e/ui_e2e.mjs` with a deep-link scenario: after an upload that leaves unknown clusters, navigate to `BASE + "/?meeting=" + id + "&task=speakers&batch=" + batchId`, wait for `[data-testid="view-wizard"].active`, assert the audio element has a `src`, choose "新建", fill name, submit, then assert the meeting's `unknown_clusters` is empty and `handoff.needed` is false. Do NOT change the nav-button-count assertion.
- [ ] **Step 7:** Run `uv run pytest -q` (all green) and, if the real service + fixtures are available, `tests/e2e/run-e2e.sh`.

---

### Task 4: Meeting history inline rename

**Files:** `ui_static/app.js`

**Deliverable:** The meeting-history row title is editable (click → input → `PATCH /meetings/{id}` `{title}`).

- [ ] **Step 1:** In `renderMeetings()`, render the title cell with `data-testid="meeting-title-{id}"` and make it click-editable (same pattern as the speaker inline edit); on save `PATCH /meetings/{id}` and `await loadMeetings()`.
- [ ] **Step 2:** Run `uv run pytest -q` → PASS.

---

### Task 5: UI contract tests

**Files:** `tests/test_ui.py`

- [ ] **Step 1:** Assert the wizard + intro testids exist and `data-view=` count is 5 (Tasks 1–2 already add these).
- [ ] **Step 2:** Re-run `uv run pytest tests/test_ui.py -q` and the full `uv run pytest -q`.

---

## Self-Review

- Spec §6.2 wizard (one-at-a-time, audio, attach/new/skip, remember gate) → Tasks 2–3. §7.1 intro header → Task 1. §7.2 editable title (detail + history) → Tasks 1, 4. §7.3 speaker job title → Task 1. Deep link §5.3 → Task 2.
- Deferred to P2: summary population (Task 1 shows a placeholder), consent records, voice-library quality/export.
- Constraint honored: no sixth nav tab. Testids preserved.
- Placeholder scan: markup + behavior are concrete; copy strings given.

## Execution Handoff

Delegate to a `visual-engineering` subagent with the `frontend` skill, or execute inline. Either way, verification is `uv run pytest -q` plus (if available) the Playwright e2e; then an Oracle UI/UX review.
