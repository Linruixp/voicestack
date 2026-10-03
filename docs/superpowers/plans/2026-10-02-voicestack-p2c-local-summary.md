# VoiceStack P2c · Local Chinese meeting summary — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** Summarize a Chinese meeting transcript **locally** (Ollama + Qwen3.5-9B) into structured JSON — TL;DR, decisions, action items (owner/due) and chapters — and show it above the transcript.

**Architecture:** A pluggable `SummaryBackend` (Ollama HTTP, JSON-schema-constrained) behind `ServiceDeps`; a `POST /meetings/{id}/summary` route runs it in a threadpool and stores `meetings.summary_json` (raw CJK, `ensure_ascii=False`); `meeting_detail` exposes the parsed `summary`; the vanilla UI renders it in the intro header with a "生成摘要" button. Tests use a fake backend — **no model needed**.

**Tech Stack:** Python 3.12, FastAPI, httpx, Ollama (`qwen3.5:9b`), vanilla JS.

## Global Constraints

- Work dir: `/Users/LinRui/voicestack/speaker-service`; tests via `uv run pytest ...`.
- **Do not commit** unless the user explicitly approves.
- **DOWNLOAD GATE (user rule):** installing Ollama and pulling `qwen3.5:9b` (~6.6 GB) MUST NOT start without first pausing to tell the user to switch to a domestic mirror/network; notify them when done so they can switch back. Tasks 1–3 are offline and precede this.
- Store `summary_json` with `ensure_ascii=False` so Chinese is searchable (P2b N5).
- Summary is a draft: label it "AI 生成，可编辑"; never fabricate an owner/due (use null).

---

### Task 1 (offline): Summary schema + pluggable backend

**Files:** `config.py`, `summarizer.py` (new); Test: `tests/test_summarizer.py`

**Interfaces:**
- Produces:
  - `Settings`: `ollama_base_url="http://localhost:11434"`, `summary_model="qwen3.5:9b"`, `summary_timeout_s=600.0`
  - `summarizer.SummaryPayload` (pydantic): `tldr: str`, `decisions: list[str]`, `action_items: list[ActionItem{text, owner|None, due|None}]`, `chapters: list[Chapter{title, start}]`
  - `summarizer.SUMMARY_SCHEMA` (JSON schema for Ollama `format`)
  - `summarizer.OllamaBackend(base_url, model, timeout_s).generate(*, system, user) -> str`
  - `summarizer.build_transcript(segments, names) -> str`
  - `summarizer.summarize_meeting(registry, meeting_id, backend) -> dict`

- [ ] **Step 1: Test** — `tests/test_summarizer.py`:

```python
from __future__ import annotations

from pathlib import Path

from registry import NewSegment, open_registry
import summarizer


class FakeBackend:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.seen: str | None = None

    def generate(self, *, system: str, user: str) -> str:
        self.seen = user
        return self.payload


def test_summarize_meeting_parses_and_persists(db_path: Path) -> None:
    payload = (
        '{"tldr":"讨论了排期","decisions":["下周上线"],'
        '"action_items":[{"text":"写测试","owner":"张三","due":null}],'
        '"chapters":[{"title":"排期","start":0.0}]}'
    )
    with open_registry(db_path) as registry:
        meeting_id = registry.create_meeting("规划会")
        registry.add_segment(meeting_id, NewSegment(0.0, 2.0, "我们讨论排期", None, None))
        backend = FakeBackend(payload)
        result = summarizer.summarize_meeting(registry, meeting_id, backend)
        assert result["tldr"] == "讨论了排期"
        assert result["action_items"][0]["owner"] == "张三"
        # And: raw Chinese is stored unescaped so P2b search can match it
        stored = registry.get_meeting(meeting_id).summary_json
        assert "讨论了排期" in stored and "\\u" not in stored
        assert "我们讨论排期" in backend.seen
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `config.py` (three settings) and `summarizer.py` with pydantic models, `SUMMARY_SCHEMA` (from `SummaryPayload.model_json_schema()`), a Chinese system prompt ("仅输出 JSON；owner/due 未知用 null；章节 start 用秒数；基于转写，不要编造"), `OllamaBackend.generate` (httpx POST `{base}/api/chat` with `stream:false`, `format=SUMMARY_SCHEMA`, `options.temperature=0.2`, `raise_for_status`, return `message.content`), `build_transcript` (`[mm:ss] 说话人：文本` lines), and `summarize_meeting` (validate JSON → `update_meeting(summary_json=payload.model_dump_json())` → return dict).

- [ ] **Step 4: Run** → PASS.

---

### Task 2 (offline): `POST /meetings/{id}/summary` + expose `summary`

**Files:** `api_schemas.py` (optional), `api_service.py`, `api_routes.py`, `app.py`, `tests/conftest.py`; Test: `tests/test_api.py`

**Interfaces:**
- Produces: `ServiceDeps.summarizer: SummaryBackend`; `meeting_detail` gains `summary`; `POST /meetings/{id}/summary` → the summary dict.

- [ ] **Step 1: Test** — add to `tests/test_api.py`:

```python
def test_generate_summary_endpoint(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    meeting = upload_meeting(client)
    response = client.post(f"/meetings/{meeting['meeting_id']}/summary", headers=auth)
    assert response.status_code == 200
    assert response.json()["tldr"] == "fake summary"
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["summary"]["tldr"] == "fake summary"
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — add `summarizer: SummaryBackend` to `ServiceDeps`; in `create_app` build `OllamaBackend(settings.ollama_base_url, settings.summary_model, settings.summary_timeout_s)` by default (injectable). `api_service.meeting_detail` parses `summary_json` (json.loads, None on empty). Route `POST /meetings/{id}/summary` opens the registry, and runs `summarizer.summarize_meeting` in `run_in_threadpool` (blocking model call). `conftest.py` injects a `FakeBackend` returning a fixed JSON payload (e.g. `{"tldr":"fake summary","decisions":[],"action_items":[],"chapters":[]}`) via `make_app`/`create_app`.

- [ ] **Step 4: Run** → `uv run pytest tests/test_api.py -q` → PASS.

---

### Task 3 (offline): Intro summary UI

**Files:** `ui_static/index.html`, `ui_static/app.js`, `ui_static/app.css`; Test: `tests/test_ui.py`

**Interfaces:**
- Produces: `#transcript-summary` renders TL;DR / decisions / action items (owner·due) / chapters (click→seek if audio) or a "生成摘要" button (`data-testid="generate-summary"`).

- [ ] **Step 1:** `renderIntro`: if `state.detail.summary` → render sections (escaped) into `#transcript-summary`; else render the button.
- [ ] **Step 2:** Button handler → `POST /meetings/{id}/summary` with a "生成中…（本地模型，可能要一两分钟）" status; on success store into `state.detail.summary`, re-render; errors → status.
- [ ] **Step 3:** CSS for summary sections/action items/chapters.
- [ ] **Step 4:** `tests/test_ui.py`: assert `generate-summary` and summary section testids appear in `app.js`.
- [ ] **Step 5:** Run `uv run pytest -q` + `node --check`.

---

### Task 4 (GATED download): install Ollama + pull `qwen3.5:9b`, verify end-to-end

- [ ] **Step 1:** **PAUSE.** Tell the user: "即将安装 Ollama 并拉取 qwen3.5:9b（约 6.6GB）。请切到国内网络/镜像后告诉我。" Do not proceed until they confirm.
- [ ] **Step 2:** Install Ollama and `ollama pull qwen3.5:9b` (using the user's mirror/network). 
- [ ] **Step 3:** Notify the user the download finished so they can switch back to the international network.
- [ ] **Step 4:** Real smoke: run the service and summarize a Chinese meeting; confirm structured Chinese JSON and that `GET /meetings?q=<中文摘要词>` matches (P2b N5).

---

## Self-Review

- Spec §8 (`SummarizerBackend`, Ollama+qwen3.5:9b, structured JSON, async-ish, editable draft) → Tasks 1–3; the download gate → Task 4. §7.1 summary in the intro header → Task 3.
- Citations (`source_segment_ids`) are best-effort/deferred: the schema omits per-item ids to keep the constrained JSON reliable; chapters carry `start` timestamps for seek.
- Deepasync job/queue deferred: the route blocks in a threadpool with a client spinner (local single-user).
- Placeholder scan: concrete code.

## Execution Handoff

Inline execution. Tasks 1–3 offline now; **Task 4 pauses for the user's network switch** before any >1 GB download. Oracle review after Task 2 and at the end (recover via `session_read` if needed).
