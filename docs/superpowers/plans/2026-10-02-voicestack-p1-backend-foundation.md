# VoiceStack P1 · Backend Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add schema v2 (meeting metadata + speaker job title), make meeting titles editable end-to-end (registry → HTTP → MCP), and thread the speaker `title` field through enrollment — the backend foundation the P1 handoff and UI plans build on.

**Architecture:** Extend the existing SQLite registry in place with additive, nullable columns and a v1→v2 migration (which already backs up before migrating). Add one registry method (`update_meeting`), one HTTP route (`PATCH /meetings/{id}`), and one MCP tool (`rename_meeting`). No new runtime dependencies.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite + sqlite-vec, MCP (`mcp==2.2.0`), pytest, `uv`.

## Global Constraints

- Work directory: `/Users/LinRui/voicestack/speaker-service`. Run tests with `uv run pytest ...`.
- **Additive-only schema changes**: every new column is nullable; existing v1 databases must open and keep all data.
- **Commit policy (user rule):** the repo owner has **not** authorized commits. Treat each "Commit" step as a **checkpoint** — do NOT run `git commit` unless the user explicitly approves; if approved, use the exact message shown.
- New columns on `Meeting` / `Speaker` must have **defaults** so existing constructor calls keep compiling.
- Do not introduce new third-party dependencies in P1.
- Keep each source file focused; do not unilaterally restructure existing modules.
- The MCP server stays a thin proxy; it never opens the SQLite registry directly.

---

### Task 1: Schema v2 migration

**Files:**
- Modify: `registry_schema.py` (the `SCHEMA_VERSION` constant, `_apply_schema`, `_migrate`)
- Test: `tests/test_registry_migrations.py`

**Interfaces:**
- Consumes: existing `registry_schema._SCHEMA_V1`, `_backup`, `connect`, `SCHEMA_VERSION`.
- Produces: `registry_schema.SCHEMA_VERSION == 2`; `meetings` gains `topic, location, duration_s, summary_json, original_title`; `speakers` gains `title`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_registry_migrations.py`:

```python
from registry_schema import _SCHEMA_V1  # noqa: PLC2701 - test needs the v1 DDL verbatim


def test_open_migrates_v1_database_to_v2_preserving_rows(tmp_path: Path) -> None:
    # Given: a database stamped at v1 (pre-v2) with one speaker row
    v1_path = tmp_path / "v1.db"
    legacy = sqlite3.connect(v1_path)
    legacy.executescript(_SCHEMA_V1)
    legacy.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')"
    )
    legacy.execute("INSERT INTO speakers(name) VALUES ('Alice')")
    legacy.commit()
    legacy.close()
    # When: the registry opens it (triggering the v1 -> v2 migration)
    with open_registry(v1_path) as registry:
        assert registry.schema_version() == 2
        # Then: pre-existing rows survive and the new nullable columns exist
        alice = registry.list_speakers()[0]
        assert alice.name == "Alice"
        assert alice.title is None
        meeting_id = registry.create_meeting(
            "Kickoff", date="2026-01-01", duration_s=12.5
        )
        meeting = registry.get_meeting(meeting_id)
        assert meeting is not None
        assert meeting.duration_s == 12.5
        assert meeting.location is None
        assert meeting.original_title == "Kickoff"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry_migrations.py::test_open_migrates_v1_database_to_v2_preserving_rows -v`
Expected: FAIL — `TypeError`/`sqlite3.OperationalError` (no `duration_s` column; `create_meeting` has no such kwarg).

- [ ] **Step 3: Implement the migration**

In `registry_schema.py`, change the version and add the v2 DDL plus migration branch:

```python
SCHEMA_VERSION: Final = 2
```

Add after `_SCHEMA_V1`:

```python
_SCHEMA_V2: Final = """
ALTER TABLE meetings ADD COLUMN topic TEXT;
ALTER TABLE meetings ADD COLUMN location TEXT;
ALTER TABLE meetings ADD COLUMN duration_s REAL;
ALTER TABLE meetings ADD COLUMN summary_json TEXT;
ALTER TABLE meetings ADD COLUMN original_title TEXT;

ALTER TABLE speakers ADD COLUMN title TEXT;
"""
```

Replace `_apply_schema` and `_migrate`:

```python
def _apply_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(_SCHEMA_V1)
        conn.executescript(_SCHEMA_V2)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _utc_now()),
        )


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version < 1:
        # A legacy/unstamped file: create the full current schema.
        _apply_schema(conn)
    elif from_version < 2:
        with conn:
            conn.executescript(_SCHEMA_V2)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (2, _utc_now()),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_registry_migrations.py -v`
Expected: PASS (both the new test and the existing v0 test that asserts `schema_version() == SCHEMA_VERSION`).

- [ ] **Step 5: Checkpoint (commit only if user approved)**

```bash
git add speaker-service/registry_schema.py speaker-service/tests/test_registry_migrations.py
git commit -m "feat(registry): schema v2 with meeting metadata and speaker title"
```

---

### Task 2: Meeting domain fields + registry operations

**Files:**
- Modify: `registry_models.py` (`Meeting` dataclass)
- Modify: `registry_support.py` (`meeting_from_row`)
- Modify: `registry_meetings.py` (`create_meeting`, `get_meeting`, `list_meetings`, new `update_meeting`)
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: Task 1's v2 columns.
- Produces:
  - `Meeting(id, title, date, audio_path, created_at, topic=None, location=None, duration_s=None, summary_json=None, original_title=None)`
  - `MeetingOps.create_meeting(title, date=None, audio_path=None, *, topic=None, location=None, duration_s=None, original_title=None) -> int`
  - `MeetingOps.update_meeting(meeting_id, *, title=None, topic=None, location=None, duration_s=None, summary_json=None) -> bool`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_registry.py` (uses the same `open_registry(tmp_path / "r.db")` fixture style already in that file):

```python
def test_update_meeting_edits_fields_and_preserves_original_title(tmp_path) -> None:
    with open_registry(tmp_path / "r.db") as registry:
        meeting_id = registry.create_meeting("recording", audio_path="/a.m4a")
        # original_title is captured from the creation title
        assert registry.get_meeting(meeting_id).original_title == "recording"
        # When: the title is edited
        assert registry.update_meeting(meeting_id, title="Weekly sync") is True
        updated = registry.get_meeting(meeting_id)
        # Then: the new title wins and the original is retained
        assert updated.title == "Weekly sync"
        assert updated.original_title == "recording"
        # And: metadata can be set independently
        assert registry.update_meeting(
            meeting_id, location="Room 3", topic="Roadmap", duration_s=901.5
        ) is True
        assert registry.get_meeting(meeting_id).location == "Room 3"
        assert registry.get_meeting(meeting_id).duration_s == 901.5


def test_update_meeting_unknown_id_returns_false(tmp_path) -> None:
    with open_registry(tmp_path / "r.db") as registry:
        assert registry.update_meeting(999, title="x") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry.py::test_update_meeting_edits_fields_and_preserves_original_title -v`
Expected: FAIL — `AttributeError: 'Registry' object has no attribute 'update_meeting'`.

- [ ] **Step 3: Extend the `Meeting` value object**

In `registry_models.py`, replace the `Meeting` dataclass:

```python
@dataclass(frozen=True, slots=True)
class Meeting:
    id: int
    title: str
    date: str | None
    audio_path: str | None
    created_at: str
    topic: str | None = None
    location: str | None = None
    duration_s: float | None = None
    summary_json: str | None = None
    original_title: str | None = None
```

- [ ] **Step 4: Extend the row mapper**

In `registry_support.py`, replace `meeting_from_row`:

```python
def meeting_from_row(row: sqlite3.Row) -> Meeting:
    return Meeting(
        id=int(row["id"]),
        title=str(row["title"]),
        date=row["date"],
        audio_path=row["audio_path"],
        created_at=str(row["created_at"]),
        topic=row["topic"],
        location=row["location"],
        duration_s=row["duration_s"],
        summary_json=row["summary_json"],
        original_title=row["original_title"],
    )
```

- [ ] **Step 5: Extend create/get/list and add update_meeting**

In `registry_meetings.py`, replace `create_meeting`, `get_meeting`, `list_meetings`, and append `update_meeting`:

```python
    _MEETING_COLUMNS = (
        "id, title, date, audio_path, created_at, topic, location,"
        " duration_s, summary_json, original_title"
    )

    def create_meeting(
        self,
        title: str,
        date: str | None = None,
        audio_path: str | None = None,
        *,
        topic: str | None = None,
        location: str | None = None,
        duration_s: float | None = None,
        original_title: str | None = None,
    ) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO meetings(title, date, audio_path, topic, location,"
                " duration_s, original_title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    title,
                    date,
                    audio_path,
                    topic,
                    location,
                    duration_s,
                    original_title if original_title is not None else title,
                ),
            )
        return last_id(cursor)

    def get_meeting(self, meeting_id: int) -> Meeting | None:
        row = self._conn.execute(
            f"SELECT {self._MEETING_COLUMNS} FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
        return meeting_from_row(row) if row is not None else None

    def list_meetings(self) -> list[Meeting]:
        rows = self._conn.execute(
            f"SELECT {self._MEETING_COLUMNS} FROM meetings ORDER BY id"
        ).fetchall()
        return [meeting_from_row(row) for row in rows]

    def update_meeting(
        self,
        meeting_id: int,
        *,
        title: str | None = None,
        topic: str | None = None,
        location: str | None = None,
        duration_s: float | None = None,
        summary_json: str | None = None,
    ) -> bool:
        """Patch meeting metadata; on the first title edit keep ``original_title``."""
        assignments: list[str] = []
        params: list[object] = []
        if title is not None:
            assignments.append("original_title = COALESCE(original_title, title)")
            assignments.append("title = ?")
            params.append(title)
        if topic is not None:
            assignments.append("topic = ?")
            params.append(topic)
        if location is not None:
            assignments.append("location = ?")
            params.append(location)
        if duration_s is not None:
            assignments.append("duration_s = ?")
            params.append(duration_s)
        if summary_json is not None:
            assignments.append("summary_json = ?")
            params.append(summary_json)
        if not assignments:
            return self.get_meeting(meeting_id) is not None
        params.append(meeting_id)
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE meetings SET {', '.join(assignments)} WHERE id = ?",
                (*params,),
            )
        return cursor.rowcount > 0
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: PASS.

- [ ] **Step 7: Checkpoint (commit only if user approved)**

```bash
git add speaker-service/registry_models.py speaker-service/registry_support.py speaker-service/registry_meetings.py speaker-service/tests/test_registry.py
git commit -m "feat(registry): meeting metadata fields and update_meeting"
```

---

### Task 3: Speaker `title` field through registry and enrollment

**Files:**
- Modify: `registry_models.py` (`Speaker` dataclass)
- Modify: `registry_support.py` (`speaker_from_row`)
- Modify: `registry_speakers.py` (`add_speaker`, `get_speaker`, `list_speakers`)
- Modify: `enrollment.py` (`enroll_speaker` signature + `add_speaker` call)
- Modify: `api_operations.py` (`enroll_new_speaker`)
- Test: `tests/test_enroll.py`

**Interfaces:**
- Consumes: Task 1's `speakers.title` column.
- Produces:
  - `Speaker(id, name, organization, notes, created_at, title=None)`
  - `SpeakerOps.add_speaker(name, organization=None, notes=None, title=None) -> int`
  - `enrollment.enroll_speaker(..., organization=None, notes=None, title=None)`
  - `api_operations.enroll_new_speaker(..., title=None)`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_enroll.py` (match the existing fixture/imports in that file for `registry`, `deps`, and an enrolled standard fixture):

```python
def test_enroll_speaker_stores_job_title(registry, deps, meeting_with_unknown_cluster):
    meeting_id, cluster_id = meeting_with_unknown_cluster
    result = enrollment.enroll_speaker(
        registry,
        deps.embedder,
        deps.audio_loader,
        name="张三",
        meeting_id=meeting_id,
        cluster_id=cluster_id,
        organization="某某科技",
        title="产品总监",
    )
    stored = registry.get_speaker(result.speaker_id)
    assert stored is not None
    assert stored.organization == "某某科技"
    assert stored.title == "产品总监"
```

> If the existing test module does not already expose a `meeting_with_unknown_cluster` fixture, reuse whatever fixture it uses to obtain a `(meeting_id, cluster_id)` with enough voiced audio; the assertion (title persisted) is the part under test.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enroll.py::test_enroll_speaker_stores_job_title -v`
Expected: FAIL — `TypeError: enroll_speaker() got an unexpected keyword argument 'title'`.

- [ ] **Step 3: Extend the `Speaker` value object**

In `registry_models.py`, replace the `Speaker` dataclass:

```python
@dataclass(frozen=True, slots=True)
class Speaker:
    id: int
    name: str
    organization: str | None
    notes: str | None
    created_at: str
    title: str | None = None
```

- [ ] **Step 4: Extend the row mapper**

In `registry_support.py`, replace `speaker_from_row`:

```python
def speaker_from_row(row: sqlite3.Row) -> Speaker:
    return Speaker(
        id=int(row["id"]),
        name=str(row["name"]),
        organization=row["organization"],
        notes=row["notes"],
        created_at=str(row["created_at"]),
        title=row["title"],
    )
```

- [ ] **Step 5: Extend speaker registry operations**

In `registry_speakers.py`, replace `add_speaker`, `get_speaker`, `list_speakers`:

```python
    def add_speaker(
        self,
        name: str,
        organization: str | None = None,
        notes: str | None = None,
        title: str | None = None,
    ) -> int:
        clean = name.strip()
        if not clean:
            raise RegistryError("speaker name must not be empty")
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO speakers(name, organization, notes, title)"
                " VALUES (?, ?, ?, ?)",
                (clean, organization, notes, title),
            )
        return last_id(cursor)

    def get_speaker(self, speaker_id: int) -> Speaker | None:
        row = self._conn.execute(
            "SELECT id, name, organization, notes, created_at, title"
            " FROM speakers WHERE id = ?",
            (speaker_id,),
        ).fetchone()
        return speaker_from_row(row) if row is not None else None

    def list_speakers(self) -> list[Speaker]:
        rows = self._conn.execute(
            "SELECT id, name, organization, notes, created_at, title"
            " FROM speakers ORDER BY id"
        ).fetchall()
        return [speaker_from_row(row) for row in rows]
```

- [ ] **Step 6: Thread `title` through enrollment**

In `enrollment.py`, change `enroll_speaker` to accept and pass `title`:

```python
def enroll_speaker(
    registry: Registry,
    embedder: Embedder,
    audio_loader: AudioLoader,
    *,
    name: str,
    meeting_id: int,
    cluster_id: int,
    organization: str | None = None,
    notes: str | None = None,
    title: str | None = None,
) -> EnrolledSpeaker:
    """Create a NEW speaker from a cluster; the voiceprint guard runs first."""
    meeting = _require_cluster(registry, meeting_id, cluster_id)
    segments = _segments_of(registry, meeting_id, cluster_id)
    voiceprint = cluster_voiceprint(
        embedder,
        audio_loader,
        _require_audio(meeting),
        segments,
        cluster_id=cluster_id,
    )
    speaker_id = registry.add_speaker(name, organization, notes, title)
    # ... unchanged from here ...
```

In `api_operations.py`, change `enroll_new_speaker` to accept and forward `title`:

```python
def enroll_new_speaker(
    registry: Registry,
    deps: ServiceDeps,
    *,
    name: str,
    organization: str | None,
    notes: str | None,
    title: str | None,
    cluster_id: int,
) -> dict[str, Any]:
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(400, "speaker name must not be empty")
    meeting_id = resolve_cluster_meeting(registry, cluster_id)
    try:
        result = enrollment.enroll_speaker(
            registry,
            deps.embedder,
            deps.audio_loader,
            name=cleaned,
            meeting_id=meeting_id,
            cluster_id=cluster_id,
            organization=organization,
            notes=notes,
            title=title,
        )
    except RegistryError as exc:
        raise _translate(exc) from exc
    return {
        "speaker_id": result.speaker_id,
        "voiceprint_id": result.voiceprint_id,
        "cluster_id": result.cluster_id,
        "name": result.name,
    }
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_enroll.py tests/test_registry.py -v`
Expected: PASS.

- [ ] **Step 8: Checkpoint (commit only if user approved)**

```bash
git add speaker-service/registry_models.py speaker-service/registry_support.py speaker-service/registry_speakers.py speaker-service/enrollment.py speaker-service/api_operations.py speaker-service/tests/test_enroll.py
git commit -m "feat(speakers): job title field through enrollment"
```

---

### Task 4: HTTP API — meeting metadata, editable title, speaker title

**Files:**
- Modify: `api_schemas.py` (`MeetingPatch`; add `title` to `SpeakerCreate`, `SpeakerPatch`, `EnrollRequest`)
- Modify: `api_service.py` (`meeting_payload`, `speaker_payload`)
- Modify: `api_operations.py` (`update_meeting` adapter)
- Modify: `api_routes.py` (`PATCH /meetings/{meeting_id}`)
- Modify: `api_speaker_routes.py` (pass `title` on enroll)
- Test: `tests/test_api.py`, `tests/test_api_speakers.py`

**Interfaces:**
- Consumes: Task 2 `update_meeting`, Task 3 speaker `title`.
- Produces:
  - `PATCH /meetings/{id}` accepting `{title?, topic?, location?}` → meeting payload
  - meeting payload keys: `id, title, date, audio_path, created_at, topic, location, duration_s, summary_json, original_title`
  - speaker payload keys: `..., title`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py` (the module already builds a `TestClient` with a temp registry; reuse that `client` fixture):

```python
def test_patch_meeting_edits_title_and_metadata(client, seeded_meeting):
    meeting_id = seeded_meeting
    resp = client.patch(
        f"/meetings/{meeting_id}",
        json={"title": "Weekly sync", "location": "Room 3", "topic": "Roadmap"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Weekly sync"
    assert body["location"] == "Room 3"
    assert body["topic"] == "Roadmap"
    assert body["original_title"]  # captured at creation


def test_patch_meeting_unknown_returns_404(client):
    resp = client.patch("/meetings/999", json={"title": "x"})
    assert resp.status_code == 404
```

> If `tests/test_api.py` has no `seeded_meeting` fixture, obtain a meeting id the same way the existing meeting tests in that file do (upload/`create_meeting`).

Add to `tests/test_api_speakers.py`:

```python
def test_patch_speaker_accepts_title(client, seeded_speaker):
    speaker_id = seeded_speaker
    resp = client.patch(f"/speakers/{speaker_id}", json={"title": "产品总监"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "产品总监"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api.py::test_patch_meeting_edits_title_and_metadata tests/test_api_speakers.py::test_patch_speaker_accepts_title -v`
Expected: FAIL — PATCH `/meetings/{id}` returns 405; speaker payload has no `title`.

- [ ] **Step 3: Add request schemas**

In `api_schemas.py`, add `MeetingPatch` and extend the speaker schemas:

```python
class MeetingPatch(_Strict):
    """Only fields present in the request body are applied."""

    title: str | None = None
    topic: str | None = None
    location: str | None = None
```

Add `title: str | None = None` to `SpeakerCreate`, `SpeakerPatch`, and `EnrollRequest`.

- [ ] **Step 4: Extend the payloads**

In `api_service.py`, replace `speaker_payload` and `meeting_payload`:

```python
def speaker_payload(speaker: Speaker, voiceprint_count: int) -> dict[str, Any]:
    return {
        "id": speaker.id,
        "name": speaker.name,
        "organization": speaker.organization,
        "notes": speaker.notes,
        "title": speaker.title,
        "created_at": speaker.created_at,
        "voiceprint_count": voiceprint_count,
    }


def meeting_payload(meeting: Meeting) -> dict[str, Any]:
    return {
        "id": meeting.id,
        "title": meeting.title,
        "date": meeting.date,
        "audio_path": meeting.audio_path,
        "created_at": meeting.created_at,
        "topic": meeting.topic,
        "location": meeting.location,
        "duration_s": meeting.duration_s,
        "summary_json": meeting.summary_json,
        "original_title": meeting.original_title,
    }
```

- [ ] **Step 5: Add the meeting update adapter**

In `api_operations.py`, add (import `meeting_payload` from `api_service`):

```python
def update_meeting(
    registry: Registry, meeting_id: int, changes: dict[str, Any]
) -> dict[str, Any]:
    if not changes:
        raise HTTPException(400, "no fields to update")
    if "title" in changes:
        changes["title"] = (changes["title"] or "").strip()
        if not changes["title"]:
            raise HTTPException(400, "meeting title must not be empty")
    if not registry.update_meeting(
        meeting_id,
        title=changes.get("title"),
        topic=changes.get("topic"),
        location=changes.get("location"),
    ):
        raise HTTPException(404, f"meeting {meeting_id} does not exist")
    meeting = registry.get_meeting(meeting_id)
    assert meeting is not None
    from api_service import meeting_payload

    return meeting_payload(meeting)
```

Change the import block at the top of `api_operations.py` from `from api_service import ServiceDeps, speaker_payload` to also import `meeting_payload` (then remove the local import above):

```python
from api_service import ServiceDeps, meeting_payload, speaker_payload
```

- [ ] **Step 6: Add the route**

In `api_routes.py`, import `MeetingPatch` and `update_meeting`, then add after `get_meeting`:

```python
@router.patch("/meetings/{meeting_id}", dependencies=[Depends(require_mutation_auth)])
def patch_meeting(
    request: Request, meeting_id: int, body: MeetingPatch
) -> dict[str, object]:
    with request.app.state.registry_factory() as registry:
        return update_meeting(registry, meeting_id, body.model_dump(exclude_unset=True))
```

Update the imports:

```python
from api_operations import split_cluster, update_meeting
from api_schemas import IdentifyRequest, MeetingPatch, SplitRequest
```

- [ ] **Step 7: Pass speaker title on enroll**

In `api_speaker_routes.py`, pass the new field:

```python
        return enroll_new_speaker(
            registry,
            request.app.state.deps,
            name=body.name,
            organization=body.organization,
            notes=body.notes,
            title=body.title,
            cluster_id=body.cluster_id,
        )
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_api.py tests/test_api_speakers.py -v`
Expected: PASS.

- [ ] **Step 9: Checkpoint (commit only if user approved)**

```bash
git add speaker-service/api_schemas.py speaker-service/api_service.py speaker-service/api_operations.py speaker-service/api_routes.py speaker-service/api_speaker_routes.py speaker-service/tests/test_api.py speaker-service/tests/test_api_speakers.py
git commit -m "feat(api): editable meeting title, metadata, and speaker title"
```

---

### Task 5: Pipeline populates `date` and `duration_s`

**Files:**
- Modify: `pipeline.py` (`transcribe_meeting`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Task 2 `create_meeting(..., duration_s=)` and `update_meeting`.
- Produces: meetings get a non-null `date` (from the file mtime) and `duration_s` equal to the last segment end.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline.py` (reuse the module's existing "run on a fixture audio" helper; assert on the persisted meeting):

```python
def test_transcribe_sets_meeting_date_and_duration(registry, sample_audio_path):
    result = pipeline.transcribe_meeting(
        sample_audio_path, meeting_title="Standup", registry=registry
    )
    meeting = registry.get_meeting(result.meeting_id if hasattr(result, "meeting_id") else result)
    segments = registry.segments_for_meeting(meeting.id)
    assert meeting.date  # populated from file mtime
    assert meeting.duration_s == pytest.approx(max(s.end for s in segments))
```

> Use the meeting id the same way the existing pipeline test retrieves it (the `MeetingResult` accessor already used in this file). The two assertions above are the behavior under test.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py::test_transcribe_sets_meeting_date_and_duration -v`
Expected: FAIL — `meeting.date` is `None` / `duration_s` is `None`.

- [ ] **Step 3: Populate date at creation and duration after success**

In `pipeline.py`, add the import at the top:

```python
from datetime import UTC, datetime
```

Replace the meeting-creation line inside `transcribe_meeting`:

```python
        title = meeting_title or path.stem
        meeting_date = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        meeting_id = store.create_meeting(
            title, date=meeting_date, audio_path=str(path)
        )
```

After `result = _run_meeting(job, path, overrides)` and before `store.update_job(job_id, JobState.DONE)`, set the duration:

```python
        segments = store.segments_for_meeting(meeting_id)
        duration = max((segment.end for segment in segments), default=None)
        if duration is not None:
            store.update_meeting(meeting_id, duration_s=duration)
        store.update_job(job_id, JobState.DONE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS.

- [ ] **Step 5: Checkpoint (commit only if user approved)**

```bash
git add speaker-service/pipeline.py speaker-service/tests/test_pipeline.py
git commit -m "feat(pipeline): persist meeting date and duration"
```

---

### Task 6: MCP `rename_meeting`

**Files:**
- Modify: `service_client.py` (`rename_meeting`)
- Modify: `mcp_server.py` (new tool + `INSTRUCTIONS`)
- Test: `tests/test_mcp_server.py`, `tests/test_guide.py` (manifest)

**Interfaces:**
- Consumes: Task 4 `PATCH /meetings/{id}`.
- Produces: MCP tool `rename_meeting(meeting_id: int, title: str)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py` (reuse the module's fake `ServiceClient` / server-build helper):

```python
def test_rename_meeting_tool_calls_client():
    calls = {}

    class FakeClient:
        def rename_meeting(self, meeting_id, title):
            calls["args"] = (meeting_id, title)
            return {"id": meeting_id, "title": title}

    # Build the server exactly the way the other tool tests in this file do,
    # then list/invoke tools and assert:
    #   - "rename_meeting" is in the tool names
    #   - invoking it with {meeting_id: 5, title: "X"} calls the client once
    ...
    assert calls["args"] == (5, "X")
```

> Mirror the existing tool test in `tests/test_mcp_server.py` for server construction and tool invocation; the assertions above are what matters.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: FAIL — no `rename_meeting` tool.

- [ ] **Step 3: Add the client method**

In `service_client.py`, add after `rename_speaker`:

```python
    def rename_meeting(self, meeting_id: int, title: str) -> Any:
        return self._request(
            "PATCH", f"/meetings/{meeting_id}", json_body={"title": title}
        )
```

- [ ] **Step 4: Add the MCP tool**

In `mcp_server.py`, add after `rename_speaker`:

```python
    @server.tool(description="Rename an existing meeting (edits its title).")
    async def rename_meeting(meeting_id: int, title: str) -> types.CallToolResult:
        return await _dispatch(
            proxy.rename_meeting, meeting_id=meeting_id, title=title
        )
```

Update `INSTRUCTIONS` to mention it (append to the existing sentence):

```python
    "attach_to_speaker / rename_speaker to manage the speaker directory; "
    "rename_meeting to edit a meeting title; "
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py tests/test_guide.py -v`
Expected: PASS. If `tests/test_guide.py` pins the exact tool set, add `rename_meeting` to the expected list it asserts against.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all tests).

- [ ] **Step 7: Checkpoint (commit only if user approved)**

```bash
git add speaker-service/service_client.py speaker-service/mcp_server.py speaker-service/tests/test_mcp_server.py speaker-service/tests/test_guide.py
git commit -m "feat(mcp): rename_meeting tool"
```

---

## Self-Review

**Spec coverage (Plan 1 scope):**
- Spec §4.1 meetings columns + `original_title` + `date` fill + `duration_s` → Tasks 1, 2, 5.
- Spec §4.2 `speakers.title` → Tasks 1, 3.
- Spec §7.2 editable title (registry + `PATCH` + MCP `rename_meeting`) → Tasks 2, 4, 6.
- Spec §7.3 speaker `title` through enroll → Tasks 3, 4.
- Spec §10 API changes for P1 that belong to foundation (`PATCH /meetings/{id}`, extended payloads) → Task 4.
- Spec §11 MCP `rename_meeting` → Task 6.
- Deferred to Plan 2: `speaker_batches`/`batch_items`, `GET /clusters/{id}/sample`, `GET /speaker-batches/{id}`, `POST /speaker-batches/{id}/resolve`, `GET /events`, `open_speaker_ui`, handoff `ui_url`.
- Deferred to Plan 3: wizard UI, Intro Header UI, editable-title UI.
- Deferred to P2: `summary_json` population, `enroll_consents`, history search, `merge_speakers`/`split_cluster` MCP tools.

**Placeholder scan:** No TBD/TODO; every code step contains real code. The two test snippets reference existing fixtures by behavior (documented inline) rather than inventing them, because fixture names differ per module — the assertions are complete.

**Type consistency:** `create_meeting`/`update_meeting` signatures match between Task 2 definitions and Task 5 consumers; `speaker_payload`/`meeting_payload` keys match Task 4; `enroll_new_speaker(..., title)` matches its route call; `_MEETING_COLUMNS` used by all three meeting reads.

---

## Execution Handoff

Plan 1 complete and saved. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.

Which approach? (Plan 2 and Plan 3 will be written after Plan 1 is verified.)
