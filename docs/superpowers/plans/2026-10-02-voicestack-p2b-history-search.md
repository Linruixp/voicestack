# VoiceStack P2b · Meeting-history search & filters — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make meeting history searchable and filterable — free-text across title/summary/transcript, by participant, date range, and unresolved-speaker state — with richer, date-grouped rows.

**Architecture:** No new tables. Extend the meeting list query with `LIKE`-based substring search and filters; enrich the list payload with duration/participants/unknown-count; add a search + filter bar and date grouping to the vanilla history view. Folders/tags are deferred.

**Tech Stack:** Python 3.12, FastAPI, SQLite, vanilla JS.

## Global Constraints

- Work dir: `/Users/LinRui/voicestack/speaker-service`; tests via `uv run pytest ...`.
- **Do not commit** unless the user explicitly approves.
- Preserve existing testids; nav stays 5.
- Search must work for **Chinese** — see the deliberate deviation below.

## Deliberate deviation: LIKE, not FTS5

The spec (§9) suggested SQLite FTS5. Verified empirically on this host (SQLite 3.53.4) that neither built-in tokenizer serves Chinese: `unicode61` groups a whole Chinese sentence into one token (`MATCH '会议'` → 0 rows), and `trigram` requires ≥3 characters (2-character Chinese terms → 0 rows). A local single-user app has few meetings, so **`LIKE '%q%'` substring search** is correct for CJK and simpler than a custom tokenizer. FTS5 is therefore intentionally not used.

---

### Task 1: `search_meetings` in the registry

**Files:** `registry_meetings.py`; Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `MeetingOps.search_meetings(*, q=None, participant_id=None, date_from=None, date_to=None, unresolved=False) -> list[Meeting]`

- [ ] **Step 1: Failing test** — add to `tests/test_registry.py`:

```python
def test_search_meetings_filters_by_text_participant_date_and_unresolved(
    db_path: Path,
) -> None:
    with open_registry(db_path) as registry:
        alice = registry.add_speaker("Alice")
        a = registry.create_meeting("规划会", date="2026-10-01")
        b = registry.create_meeting("Retro", date="2026-09-01")
        c = registry.create_meeting("闲聊", date="2026-10-05")
        registry.add_segment(a, NewSegment(0.0, 1.0, "讨论项目进度", None, None))
        registry.add_segment(b, NewSegment(0.0, 1.0, "nothing here", None, None))
        cluster = registry.add_cluster(c)
        registry.add_meeting_speaker(ClusterLink(meeting_id=c, cluster_id=cluster))
        link_cluster = registry.add_cluster(a, state=ClusterState.NAMED)
        registry.add_meeting_speaker(
            ClusterLink(meeting_id=a, cluster_id=link_cluster, speaker_id=alice)
        )
        # Then: substring search finds by transcript and by title
        assert [m.id for m in registry.search_meetings(q="项目")] == [a]
        assert [m.id for m in registry.search_meetings(q="Retro")] == [b]
        # And: participant filter, date range and unresolved filter compose
        assert [m.id for m in registry.search_meetings(participant_id=alice)] == [a]
        assert [m.id for m in registry.search_meetings(date_from="2026-10-01")] == [a, c]
        assert [m.id for m in registry.search_meetings(unresolved=True)] == [c]
        assert [m.id for m in registry.search_meetings(q="a", unresolved=True)] == []
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** in `registry_meetings.py` (reuse `meeting_from_row`):

```python
    _MEETING_COLUMNS_M = ", ".join(
        f"m.{name.strip()}" for name in _MEETING_COLUMNS.split(",")
    )

    def search_meetings(
        self,
        *,
        q: str | None = None,
        participant_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        unresolved: bool = False,
    ) -> list[Meeting]:
        clauses: list[str] = []
        params: list[object] = []
        if q:
            like = f"%{q}%"
            clauses.append(
                "(m.title LIKE ? OR m.summary_json LIKE ? OR EXISTS"
                " (SELECT 1 FROM segments s WHERE s.meeting_id = m.id"
                " AND s.text LIKE ?))"
            )
            params += [like, like, like]
        if participant_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM meeting_speakers ms WHERE ms.meeting_id = m.id"
                " AND ms.speaker_id = ?)"
            )
            params.append(participant_id)
        if date_from is not None:
            clauses.append("m.date >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("m.date <= ?")
            params.append(date_to)
        if unresolved:
            clauses.append(
                "EXISTS (SELECT 1 FROM clusters c WHERE c.meeting_id = m.id"
                " AND c.state = 'unknown')"
            )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {self._MEETING_COLUMNS_M} FROM meetings m{where}"
            " ORDER BY m.id DESC",
            tuple(params),
        ).fetchall()
        return [meeting_from_row(row) for row in rows]
```

- [ ] **Step 4: Run** → `uv run pytest tests/test_registry.py -q` → PASS.

---

### Task 2: Rich list rows + query params on `GET /meetings`

**Files:** `api_service.py`, `api_routes.py`; Test: `tests/test_api.py`

**Interfaces:**
- Produces: `GET /meetings?q=&unresolved=1&participant_id=&date_from=&date_to=` →
  `{meetings:[{id,title,date,duration_s,created_at,location,topic,participants:[names],unknown_count}]}`

- [ ] **Step 1: Failing test** — add to `tests/test_api.py`:

```python
def test_meeting_list_search_and_filters(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    meeting = upload_meeting(client, title="项目规划")
    # Then: the row carries duration/participants/unknown_count
    rows = client.get("/meetings", headers=auth).json()["meetings"]
    row = rows[0]
    assert row["title"] == "项目规划"
    assert "participants" in row and "unknown_count" in row
    # And: title/transcript search and the unresolved filter work
    assert [m["id"] for m in client.get("/meetings?q=项目", headers=auth).json()["meetings"]] == [meeting["meeting_id"]]
    assert [m["id"] for m in client.get("/meetings?unresolved=1", headers=auth).json()["meetings"]] == [meeting["meeting_id"]]
    assert client.get("/meetings?q=zzz", headers=auth).json()["meetings"] == []
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.**
  - `api_service.py`: add

```python
def meeting_row_payload(registry: Registry, meeting: Meeting) -> dict[str, Any]:
    links = registry.meeting_speakers_for_meeting(meeting.id)
    names = {s.id: s.name for s in registry.list_speakers()}
    participants = sorted(
        {names[link.speaker_id] for link in links if link.speaker_id in names}
    )
    unknown_count = sum(
        1
        for cluster in registry.clusters_for_meeting(meeting.id)
        if cluster.state is ClusterState.UNKNOWN
    )
    payload = meeting_payload(meeting)
    payload["participants"] = participants
    payload["unknown_count"] = unknown_count
    return payload


def list_meeting_rows(registry: Registry, **filters: Any) -> list[dict[str, Any]]:
    return [meeting_row_payload(registry, m) for m in registry.search_meetings(**filters)]
```

  - `api_routes.list_meetings`: accept `q`, `unresolved`, `participant_id`, `date_from`, `date_to` (all optional) and return `{"meetings": list_meeting_rows(registry, q=q, unresolved=bool(unresolved), participant_id=participant_id, date_from=date_from, date_to=date_to)}`.

- [ ] **Step 4: Run** → `uv run pytest tests/test_api.py -q` → PASS.

---

### Task 3: History UI — search bar, filters, richer date-grouped rows

**Files:** `ui_static/index.html`, `ui_static/app.js`, `ui_static/app.css`; Test: `tests/test_ui.py`

**Interfaces:**
- Produces: `#meeting-search` (input), `#meeting-filter-unresolved` (checkbox), `#meeting-rows` rendered with date group headers and per-row duration/participants/unknown badge.

- [ ] **Step 1:** `index.html` `view-meetings`: add a filter form (search input `data-testid="meeting-search"`, unresolved checkbox `data-testid="meeting-filter-unresolved"`) above the table; keep `#meeting-rows`.
- [ ] **Step 2:** `app.js`:
  - `loadMeetings()` sends the current filters as query params: `api("/meetings" + queryString())`.
  - Bind the search input (debounced ~250ms) and checkbox to `loadMeetings()`.
  - `renderMeetings()`: group rows by date (今天 / 昨天 / `fmtDate` day), render a group header row; each row shows title (inline-editable, existing), date, duration (`fmt`), participants (joined), and an unknown badge when `unknown_count > 0`.
- [ ] **Step 3:** `app.css`: styles for the filter bar, group header rows, and the unknown badge (reuse `.badge`).
- [ ] **Step 4:** `tests/test_ui.py`: assert `meeting-search` / `meeting-filter-unresolved` testids and that `renderMeetings` uses `unknown_count` / group headers appear in `app.js`.
- [ ] **Step 5:** Run `uv run pytest -q` and `node --check ui_static/app.js`.

---

## Self-Review

- Spec §9 (search + filters + richer rows + date grouping) → Tasks 1–3. Folders **and** tags deferred (documented).
- Search is CJK-correct via LIKE (deviation justified above).
- Participant filter uses `meeting_speakers.speaker_id`; unresolved uses `clusters.state='unknown'`.
- Placeholder scan: concrete code.

## Execution Handoff

Inline execution (same protocol): checkpoints; Oracle review after Task 2 and at the end (recover via `session_read` if `background_output` reports Aborted).
