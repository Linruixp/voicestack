# VoiceStack P1 · Handoff (backend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a transcription yields `unknown_clusters >= threshold`, persist a "speaker batch", expose it (with a UI deep link) to the agent, and let the Web UI resolve it (enroll / attach / skip, with a voiceprint-remember toggle) — plus a per-cluster audio-sample endpoint the UI plays before naming.

**Architecture:** Extend the same SQLite registry (schema v2 → v3) with `speaker_batches` + `batch_items`. The pipeline creates a batch on transcription; `GET /meetings/{id}` gains `handoff` + `ui_url`; two new endpoints read/resolve a batch; one endpoint streams a short audio clip per cluster; MCP gains `open_speaker_ui`. Server remains the single source of truth; the agent polls the batch (SSE optional).

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite + sqlite-vec, MCP (`mcp==2.2.0`), pytest, `uv`.

## Global Constraints

- Work directory: `/Users/LinRui/voicestack/speaker-service`; run tests with `uv run pytest ...`.
- **Additive-only schema changes**; existing DBs must open and keep data.
- **Do not commit** unless the user explicitly approves (this repo's owner standing rule). "Commit" steps are checkpoints.
- New dataclass fields need defaults so existing constructors keep compiling.
- No new third-party dependencies.
- `speaker-service` is Python 3.12; MCP server stays a thin proxy (it may spawn `open` for `open_speaker_ui`).

---

### Task 1: Schema v3 + batch registry operations

**Files:**
- Modify: `registry_schema.py` (`SCHEMA_VERSION` → 3; `_SCHEMA_V3`; `_apply_schema`; `_migrate`; new `_add_batch_tables`)
- Modify: `registry_models.py` (`BatchState`, `SpeakerBatch`, `BatchItem`)
- Modify: `registry_support.py` (`batch_from_row`, `batch_item_from_row`)
- Create: `registry_batches.py` (`BatchOps`)
- Modify: `registry.py` (mix in `BatchOps`, export)
- Modify: `tests/test_registry_migrations.py` (literal `2` → `SCHEMA_VERSION`)
- Test: `tests/test_registry_batches.py`

**Interfaces:**
- Produces:
  - `registry_schema.SCHEMA_VERSION == 3`
  - `BatchOps.create_speaker_batch(meeting_id) -> int`
  - `BatchOps.add_batch_item(batch_id, cluster_id, *, suggested_speaker_id=None, similarity=None) -> int`
  - `BatchOps.get_speaker_batch(batch_id) -> SpeakerBatch | None`
  - `BatchOps.batch_items(batch_id) -> list[BatchItem]`
  - `BatchOps.open_batch_for_meeting(meeting_id) -> SpeakerBatch | None`
  - `BatchOps.mark_batch_item(batch_id, cluster_id, resolution, resolved_speaker_id=None) -> bool`
  - `BatchOps.close_batch(batch_id, state=BatchState.RESOLVED) -> bool`

- [ ] **Step 1: Write the failing test** — `tests/test_registry_batches.py`:

```python
from __future__ import annotations

from pathlib import Path

from registry import BatchItem, BatchState, open_registry


def _meeting(registry) -> tuple[int, int, int]:
    meeting_id = registry.create_meeting("M")
    first = registry.add_cluster(meeting_id)
    second = registry.add_cluster(meeting_id)
    return meeting_id, first, second


def test_batch_lifecycle_and_open_lookup(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        meeting_id, first, second = _meeting(registry)
        batch_id = registry.create_speaker_batch(meeting_id)
        registry.add_batch_item(batch_id, first, similarity=0.42)
        registry.add_batch_item(batch_id, second)
        batch = registry.get_speaker_batch(batch_id)
        assert batch is not None and batch.meeting_id == meeting_id
        assert batch.state is BatchState.OPEN
        assert [item.cluster_id for item in registry.batch_items(batch_id)] == [
            first,
            second,
        ]
        assert registry.open_batch_for_meeting(meeting_id).id == batch_id
        assert registry.mark_batch_item(batch_id, first, "enrolled") is True
        assert registry.close_batch(batch_id) is True
        assert registry.open_batch_for_meeting(meeting_id) is None
        assert registry.get_speaker_batch(batch_id).state is BatchState.RESOLVED


def test_batch_items_cascade_when_meeting_deleted(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        meeting_id, first, _ = _meeting(registry)
        batch_id = registry.create_speaker_batch(meeting_id)
        registry.add_batch_item(batch_id, first)
        assert registry.delete_meeting(meeting_id) is True
        assert registry.get_speaker_batch(batch_id) is None
        assert registry.batch_items(batch_id) == []
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'BatchState'`).

- [ ] **Step 3: Implement** — in `registry_schema.py`:

```python
SCHEMA_VERSION: Final = 3
```

after `_V2_COLUMNS` add:

```python
_SCHEMA_V3: Final = """
CREATE TABLE IF NOT EXISTS speaker_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'resolved', 'dismissed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_speaker_batches_meeting
    ON speaker_batches(meeting_id);

CREATE TABLE IF NOT EXISTS batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES speaker_batches(id) ON DELETE CASCADE,
    cluster_id INTEGER NOT NULL UNIQUE REFERENCES clusters(id) ON DELETE CASCADE,
    suggested_speaker_id INTEGER REFERENCES speakers(id) ON DELETE SET NULL,
    similarity REAL,
    resolution TEXT CHECK (resolution IN ('enrolled', 'attached', 'skipped')),
    resolved_speaker_id INTEGER REFERENCES speakers(id) ON DELETE SET NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id);
"""
```

`_apply_schema` → add `_add_batch_tables(conn)` before the stamp; add:

```python
def _add_batch_tables(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(_SCHEMA_V3)
```

`_migrate` → sequential:

```python
def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version < 1:
        _apply_schema(conn)
        return
    if from_version < 2:
        _add_missing_columns(conn)
    if from_version < 3:
        _add_batch_tables(conn)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _utc_now()),
        )
```

In `registry_models.py` add:

```python
class BatchState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class SpeakerBatch:
    id: int
    meeting_id: int
    state: BatchState
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class BatchItem:
    id: int
    batch_id: int
    cluster_id: int
    suggested_speaker_id: int | None
    similarity: float | None
    resolution: str | None
    resolved_speaker_id: int | None
    resolved_at: str | None
```

In `registry_support.py` add mappers:

```python
def batch_from_row(row: sqlite3.Row) -> SpeakerBatch:
    return SpeakerBatch(
        id=int(row["id"]),
        meeting_id=int(row["meeting_id"]),
        state=BatchState(row["state"]),
        created_at=str(row["created_at"]),
        resolved_at=row["resolved_at"],
    )


def batch_item_from_row(row: sqlite3.Row) -> BatchItem:
    return BatchItem(
        id=int(row["id"]),
        batch_id=int(row["batch_id"]),
        cluster_id=int(row["cluster_id"]),
        suggested_speaker_id=row["suggested_speaker_id"],
        similarity=row["similarity"],
        resolution=row["resolution"],
        resolved_speaker_id=row["resolved_speaker_id"],
        resolved_at=row["resolved_at"],
    )
```

(import `BatchItem`, `BatchState`, `SpeakerBatch` in registry_support)

Create `registry_batches.py`:

```python
"""Speaker-batch operations mixed into ``registry.Registry``."""

from __future__ import annotations

import sqlite3

from registry_models import BatchItem, BatchState, MeetingNotFoundError, SpeakerBatch
from registry_support import batch_from_row, batch_item_from_row, last_id


class BatchOps:
    _conn: sqlite3.Connection

    def create_speaker_batch(self, meeting_id: int) -> int:
        if self.get_meeting(meeting_id) is None:
            raise MeetingNotFoundError(meeting_id)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO speaker_batches(meeting_id) VALUES (?)", (meeting_id,)
            )
        return last_id(cursor)

    def add_batch_item(
        self,
        batch_id: int,
        cluster_id: int,
        *,
        suggested_speaker_id: int | None = None,
        similarity: float | None = None,
    ) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO batch_items(batch_id, cluster_id,"
                " suggested_speaker_id, similarity) VALUES (?, ?, ?, ?)",
                (batch_id, cluster_id, suggested_speaker_id, similarity),
            )
        return last_id(cursor)

    def get_speaker_batch(self, batch_id: int) -> SpeakerBatch | None:
        row = self._conn.execute(
            "SELECT id, meeting_id, state, created_at, resolved_at"
            " FROM speaker_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        return batch_from_row(row) if row is not None else None

    def batch_items(self, batch_id: int) -> list[BatchItem]:
        rows = self._conn.execute(
            "SELECT id, batch_id, cluster_id, suggested_speaker_id, similarity,"
            " resolution, resolved_speaker_id, resolved_at"
            " FROM batch_items WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [batch_item_from_row(row) for row in rows]

    def open_batch_for_meeting(self, meeting_id: int) -> SpeakerBatch | None:
        row = self._conn.execute(
            "SELECT id, meeting_id, state, created_at, resolved_at"
            " FROM speaker_batches WHERE meeting_id = ? AND state = 'open'"
            " ORDER BY id DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
        return batch_from_row(row) if row is not None else None

    def mark_batch_item(
        self,
        batch_id: int,
        cluster_id: int,
        resolution: str,
        resolved_speaker_id: int | None = None,
    ) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE batch_items SET resolution = ?, resolved_speaker_id = ?,"
                " resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                " WHERE batch_id = ? AND cluster_id = ?",
                (resolution, resolved_speaker_id, batch_id, cluster_id),
            )
        return cursor.rowcount > 0

    def close_batch(
        self, batch_id: int, state: BatchState = BatchState.RESOLVED
    ) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE speaker_batches SET state = ?,"
                " resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (state.value, batch_id),
            )
        return cursor.rowcount > 0
```

In `registry.py`: import `BatchOps` + the new models, `class Registry(SpeakerOps, MeetingOps, BatchOps)`, add to `__all__`.

Update `tests/test_registry_migrations.py`: replace the two `== 2` literals with `== SCHEMA_VERSION`.

- [ ] **Step 4: Run** → `uv run pytest tests/test_registry_batches.py tests/test_registry_migrations.py tests/test_registry.py -q` → PASS.

- [ ] **Step 5: Checkpoint (commit only if approved).**

---

### Task 2: Config + pipeline creates a batch

**Files:**
- Modify: `config.py` (`handoff_threshold`, `ui_base_url`, `resolved_ui_base_url` property)
- Modify: `pipeline.py` (`PipelineConfig.handoff_threshold`; `MeetingResult.batch_id`; create batch after a successful run)
- Test: `tests/test_pipeline.py`, `tests/test_registry_batches.py` (indirect)

**Interfaces:**
- Produces: `MeetingResult.batch_id: int | None`; `Settings.resolved_ui_base_url -> str`; `PipelineConfig.handoff_threshold: int | None`.

- [ ] **Step 1: Write the failing test** — add to `tests/test_pipeline.py`:

```python
def test_handoff_batch_created_when_unknowns_meet_threshold(tmp_path: Path) -> None:
    audio = _write_wav(tmp_path / "meeting.wav", _block(1.0, 0.5) + _block(1.0, -0.5))
    with open_registry(tmp_path / "registry.db") as registry:
        result = pipeline.transcribe_meeting(
            audio,
            "Standup",
            registry=registry,
            config=pipeline.PipelineConfig(
                transcribe=_fake_transcriber(),
                diarize=_fake_diarizer(),
                embedder=FakeEmbedder(),
                handoff_threshold=1,
            ),
        )
        assert result.batch_id is not None
        batch = registry.get_speaker_batch(result.batch_id)
        assert batch is not None and batch.meeting_id == result.meeting_id
        assert registry.open_batch_for_meeting(result.meeting_id).id == result.batch_id


def test_no_batch_when_below_threshold(tmp_path: Path) -> None:
    audio = _write_wav(tmp_path / "meeting.wav", _block(1.0, 0.5) + _block(1.0, -0.5))
    with open_registry(tmp_path / "registry.db") as registry:
        result = pipeline.transcribe_meeting(
            audio,
            "Standup",
            registry=registry,
            config=pipeline.PipelineConfig(
                transcribe=_fake_transcriber(),
                diarize=_fake_diarizer(),
                embedder=FakeEmbedder(),
                handoff_threshold=99,
            ),
        )
        assert result.batch_id is None
```

- [ ] **Step 2: Run** → FAIL (`PipelineConfig has no field handoff_threshold`).

- [ ] **Step 3: Implement.**
  - `config.py` `Settings`: add `handoff_threshold: int = 3` and `ui_base_url: str | None = None`; add property `resolved_ui_base_url`.
  - `pipeline.py`: `from dataclasses import asdict, dataclass, replace`; add `handoff_threshold: int | None = None` to `PipelineConfig`; add `batch_id: int | None = None` to `MeetingResult`; in `transcribe_meeting`, after the duration update and before `update_job(DONE)`:

```python
        threshold = int(
            get_settings().handoff_threshold
            if overrides.handoff_threshold is None
            else overrides.handoff_threshold
        )
        if result.unknown_clusters and len(result.unknown_clusters) >= threshold:
            batch_id = store.create_speaker_batch(meeting_id)
            for unknown in result.unknown_clusters:
                store.add_batch_item(
                    batch_id, unknown.cluster_id, similarity=unknown.similarity
                )
            result = replace(result, batch_id=batch_id)
```

- [ ] **Step 4: Run** → `uv run pytest tests/test_pipeline.py tests/test_registry_batches.py -q` → PASS.

- [ ] **Step 5: Checkpoint.**

---

### Task 3: `handoff` + `ui_url` on meeting reads

**Files:**
- Modify: `api_service.py` (`meeting_detail` gains `handoff`; new `meeting_result_payload`)
- Modify: `api_routes.py` (`get_meeting` + `upload_meeting` inject `ui_url`)
- Test: `tests/test_api.py`, `tests/test_api_clusters.py`

**Interfaces:**
- Produces: every meeting read includes
  `handoff = {"needed": bool, "batch_id": int | None, "unknown_count": int}` and `ui_url: str | None`.

- [ ] **Step 1: Write the failing test** — add to `tests/test_api.py`:

```python
def test_meeting_read_exposes_handoff_and_ui_url(
    client: TestClient, auth: dict[str, str], upload_meeting: Callable[..., dict]
) -> None:
    # Given: an upload whose fake pipeline produced one unknown cluster, with the
    # real pipeline threshold overridden to 1 via the app settings is not wired
    # here; the batch is created by the fake runner only if the app does so.
    # This test asserts the payload SHAPE when a batch exists.
    result = upload_meeting(client, title="Standup")
    detail = client.get(f"/meetings/{result['meeting_id']}", headers=auth).json()
    assert "handoff" in detail
    assert detail["handoff"]["unknown_count"] >= 1
    assert "ui_url" in detail
```

> The fake runner in `conftest.py` does not create a batch. To exercise the true path, extend the fake runner to create a batch when it has unknown clusters (see Step 3). Then assert `handoff["needed"] is True`, `handoff["batch_id"]` is an int, and `ui_url` contains `task=speakers`.

- [ ] **Step 2: Run** → FAIL (no `handoff` key).

- [ ] **Step 3: Implement.**
  - `conftest.fake_runner`: after creating the unknown cluster, mimic the pipeline's batch creation:

```python
    batch_id = registry.create_speaker_batch(meeting_id)
    registry.add_batch_item(batch_id, cluster_id)
    return pipeline.MeetingResult(..., batch_id=batch_id)
```

  - `api_service.meeting_detail`: add to the returned dict:

```python
        "handoff": {
            "needed": batch is not None,
            "batch_id": batch.id if batch else None,
            "unknown_count": len(unknown),
        },
```
  (compute `batch = registry.open_batch_for_meeting(meeting_id)` before the return)

  - `api_service` add:

```python
def meeting_result_payload(result: pipeline.MeetingResult, settings: Settings) -> dict[str, Any]:
    payload = result.to_dict()
    if result.batch_id is not None:
        payload["ui_url"] = (
            f"{settings.resolved_ui_base_url}/?meeting={result.meeting_id}"
            f"&task=speakers&batch={result.batch_id}"
        )
        payload["handoff"] = {
            "needed": True,
            "batch_id": result.batch_id,
            "unknown_count": len(result.unknown_clusters),
        }
    else:
        payload["ui_url"] = None
        payload["handoff"] = {
            "needed": False,
            "batch_id": None,
            "unknown_count": len(result.unknown_clusters),
        }
    return payload
```

  - `api_routes.upload_meeting`: `return meeting_result_payload(run_meeting(...), deps.settings)`.
  - `api_routes.get_meeting`: after `meeting_detail`, inject `ui_url`:

```python
        detail = meeting_detail(registry, meeting_id)
        batch_id = detail["handoff"]["batch_id"]
        if batch_id is not None:
            base = request.app.state.settings.resolved_ui_base_url
            detail["ui_url"] = (
                f"{base}/?meeting={meeting_id}&task=speakers&batch={batch_id}"
            )
        else:
            detail["ui_url"] = None
        return detail
```

- [ ] **Step 4: Run** → `uv run pytest tests/test_api.py tests/test_api_clusters.py -q` → PASS.

- [ ] **Step 5: Checkpoint.**

---

### Task 4: Batch read + resolve endpoints (with label-only path)

**Files:**
- Modify: `enrollment.py` (new `label_cluster`)
- Modify: `api_schemas.py` (`BatchResolveItem`, `BatchResolveRequest`)
- Modify: `api_operations.py` (`resolve_batch`)
- Create: `api_batch_routes.py`; mount in `app.py`
- Test: `tests/test_api_batches.py`

**Interfaces:**
- Produces:
  - `enrollment.label_cluster(registry, *, meeting_id, cluster_id, speaker_id, state=ClusterState.NAMED) -> None`
  - `GET /speaker-batches/{id}` → `{batch:{id,meeting_id,state,...}, items:[{cluster_id,label,segment_count,start,end,suggested_speaker_id,similarity,resolution,sample_url}]}`
  - `POST /speaker-batches/{id}/resolve` body `{"items":[{"cluster_id", "action":"enroll|attach|skip", "name"?, "organization"?, "title"?, "speaker_id"?, "remember"?}]}` → `{resolved:int, batch:{...}}`

- [ ] **Step 1: Write the failing test** — `tests/test_api_batches.py`:

```python
from collections.abc import Callable

from fastapi.testclient import TestClient


def _batch_id(client: TestClient, auth: dict[str, str], meeting: dict) -> int:
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["handoff"]["needed"] is True
    return detail["handoff"]["batch_id"]


def test_batch_read_lists_items(
    client: TestClient,
    auth: dict[str, str],
    upload_meeting: Callable[..., dict],
) -> None:
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    body = client.get(f"/speaker-batches/{batch_id}", headers=auth).json()
    assert body["batch"]["state"] == "open"
    item = body["items"][0]
    assert item["cluster_id"] == meeting["unknown_clusters"][0]["cluster_id"]
    assert item["sample_url"].endswith("/sample")


def test_resolve_enroll_remember_creates_speaker_with_voiceprint(
    client: TestClient,
    auth: dict[str, str],
    db_path,
    upload_meeting: Callable[..., dict],
) -> None:
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    response = client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={
            "items": [
                {
                    "cluster_id": cluster_id,
                    "action": "enroll",
                    "name": "张三",
                    "organization": "某某科技",
                    "title": "产品总监",
                    "remember": True,
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["resolved"] == 1
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["handoff"]["needed"] is False
    assert {segment["speaker_name"] for segment in detail["segments"]} == {"张三"}
    speakers = client.get("/speakers", headers=auth).json()["speakers"]
    assert speakers[0]["title"] == "产品总监"
    assert speakers[0]["voiceprint_count"] == 1


def test_resolve_enroll_without_remember_stores_no_voiceprint(
    client: TestClient,
    auth: dict[str, str],
    upload_meeting: Callable[..., dict],
) -> None:
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={
            "items": [
                {"cluster_id": cluster_id, "action": "enroll", "name": "李四"}
            ]
        },
    )
    speakers = client.get("/speakers", headers=auth).json()["speakers"]
    assert speakers[0]["name"] == "李四"
    assert speakers[0]["voiceprint_count"] == 0


def test_resolve_skip_closes_batch(
    client: TestClient,
    auth: dict[str, str],
    upload_meeting: Callable[..., dict],
) -> None:
    meeting = upload_meeting(client)
    batch_id = _batch_id(client, auth, meeting)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    client.post(
        f"/speaker-batches/{batch_id}/resolve",
        headers=auth,
        json={"items": [{"cluster_id": cluster_id, "action": "skip"}]},
    )
    detail = client.get(f"/meetings/{meeting['meeting_id']}", headers=auth).json()
    assert detail["handoff"]["needed"] is False
    assert detail["unknown_clusters"]  # cluster stays unknown, just not pending
```

- [ ] **Step 2: Run** → FAIL (404 on `/speaker-batches`).

- [ ] **Step 3: Implement.**
  - `enrollment.py` add:

```python
def label_cluster(
    registry: Registry,
    *,
    meeting_id: int,
    cluster_id: int,
    speaker_id: int,
    state: ClusterState = ClusterState.NAMED,
) -> None:
    """Label a cluster for this transcript only (no voiceprint is stored)."""
    speaker = registry.get_speaker(speaker_id)
    if speaker is None:
        raise SpeakerNotFoundError(speaker_id)
    _require_cluster(registry, meeting_id, cluster_id)
    _apply_assignment(
        registry, meeting_id, cluster_id, speaker_id, state, speaker.name, None
    )
```

  - `api_schemas.py` add:

```python
from typing import Literal


class BatchResolveItem(_Strict):
    cluster_id: int
    action: Literal["enroll", "attach", "skip"]
    name: str | None = None
    organization: str | None = None
    title: str | None = None
    speaker_id: int | None = None
    remember: bool = False


class BatchResolveRequest(_Strict):
    items: list[BatchResolveItem]
```

  - `api_operations.py` add `resolve_batch(registry, deps, batch_id, items) -> dict`:
    - 404 if batch missing; 409 if `state != open`.
    - For each item: resolve `meeting_id = resolve_cluster_meeting(registry, item.cluster_id)`; then
      - `skip` → `mark_batch_item(..., "skipped")`.
      - `enroll`:
        - `remember=True` → `enroll_new_speaker(registry, deps, name=..., organization=..., notes=None, title=..., cluster_id=...)` (creates speaker + voiceprint); resolution `"enrolled"`, resolved id = result `speaker_id`.
        - `remember=False` → `speaker_id = registry.add_speaker(name, organization, None, title)`; `enrollment.label_cluster(...)`; resolution `"enrolled"`.
      - `attach`: require `speaker_id`; `remember=True` → `attach_voiceprint(registry, deps, speaker_id, meeting_id, cluster_id)`; else `enrollment.label_cluster(registry, meeting_id=meeting_id, cluster_id=cluster_id, speaker_id=speaker_id, state=ClusterState.ATTACHED)`; resolution `"attached"`.
      - blank name on enroll → 400.
    - After all items: `close_batch(batch_id)`; return `{"resolved": n, "batch": {...}}`.
  - Create `api_batch_routes.py` mirroring `api_speaker_routes.py` (prefix `/speaker-batches`, `require_read_auth`/`require_mutation_auth`), with:
    - `GET /{batch_id}`: 404 if missing; items enriched with cluster label/count/start/end and `sample_url = f"/clusters/{cluster_id}/sample"`.
    - `POST /{batch_id}/resolve`.
  - `app.py`: `import api_batch_routes`; `app.include_router(api_batch_routes.router)`.

- [ ] **Step 4: Run** → `uv run pytest tests/test_api_batches.py tests/test_enroll.py -q` → PASS.

- [ ] **Step 5: Checkpoint.**

---

### Task 5: Audio sample endpoint

**Files:**
- Modify: `api_operations.py` (new `cluster_sample`)
- Modify: `api_routes.py` (`GET /clusters/{cluster_id}/sample`)
- Test: `tests/test_api_clusters.py`

**Interfaces:**
- Produces: `GET /clusters/{id}/sample` → `audio/wav` bytes (a short clip built from the cluster's longest 1–3 segments), 404 if the cluster/meeting/audio is missing.

- [ ] **Step 1: Write the failing test** — add to `tests/test_api_clusters.py`:

```python
def test_cluster_sample_returns_playable_wav(
    client_test, auth, upload_meeting
) -> None:
    meeting = upload_meeting(client_test)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    response = client_test.get(f"/clusters/{cluster_id}/sample", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"


def test_cluster_sample_unknown_is_404(client_test, auth) -> None:
    assert client_test.get("/clusters/999/sample", headers=auth).status_code == 404
```

> Use whatever `client`/fixture names `tests/test_api_clusters.py` already defines; its existing helper that obtains a meeting with an unknown cluster is the model.

- [ ] **Step 2: Run** → FAIL (404/405).

- [ ] **Step 3: Implement.**
  - `api_operations.py` (or a new `audio_clips.py`) add:

```python
import io
import wave

import numpy as np


def cluster_sample(
    registry: Registry, deps: ServiceDeps, cluster_id: int, *, max_seconds: float = 20.0
) -> bytes:
    cluster = registry.get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(404, f"cluster {cluster_id} does not exist")
    meeting = registry.get_meeting(cluster.meeting_id)
    if meeting is None or not meeting.audio_path or not Path(meeting.audio_path).is_file():
        raise HTTPException(404, "meeting audio is unavailable")
    members = [
        segment
        for segment in registry.segments_for_meeting(cluster.meeting_id)
        if segment.cluster_id == cluster_id
    ]
    if not members:
        raise HTTPException(404, "cluster has no segments")
    members.sort(key=lambda item: item.end - item.start, reverse=True)
    chosen = sorted(members[:3], key=lambda item: item.start)
    samples, sample_rate = deps.audio_loader(Path(meeting.audio_path))
    pieces: list[np.ndarray] = []
    budget = int(max_seconds * sample_rate)
    for segment in chosen:
        start = max(0, round(segment.start * sample_rate))
        end = min(len(samples), round(segment.end * sample_rate))
        if end > start:
            pieces.append(samples[start:end])
    if not pieces:
        raise HTTPException(404, "cluster has no voiced audio")
    clip = np.concatenate(pieces)[:budget] if pieces else np.zeros(0, dtype=np.float32)
    pcm = np.clip(clip, -1.0, 1.0)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return buffer.getvalue()
```

  - `api_routes.py`:

```python
from fastapi import Response

@router.get("/clusters/{cluster_id}/sample", dependencies=[Depends(require_read_auth)])
def cluster_audio_sample(request: Request, cluster_id: int) -> Response:
    data = cluster_sample(_deps(request), request.app.state.registry_factory, cluster_id)
    return Response(content=data, media_type="audio/wav")
```

> The helper signature is a design choice — pass the registry via a with-block like other routes rather than the factory. Implement it consistently with the other routes (open registry, call `cluster_sample(registry, deps, cluster_id)`).

- [ ] **Step 4: Run** → `uv run pytest tests/test_api_clusters.py -q` → PASS.

- [ ] **Step 5: Checkpoint.**

---

### Task 6: MCP `open_speaker_ui`

**Files:**
- Modify: `mcp_server.py` (new tool + `INSTRUCTIONS`)
- Modify: `tests/test_mcp_server.py` (`EXPECTED_TOOLS`)
- Modify: `docs/USAGE.md` (manifest + prose; 22 → 23 tools)
- Test: `tests/test_mcp_server.py`, `tests/test_guide.py`

**Interfaces:**
- Produces: MCP tool `open_speaker_ui(meeting_id: int)` → reads `GET /meetings/{id}`, opens `ui_url` (or the UI root) via the OS `open` command; returns `{"opened": url}`.

- [ ] **Step 1: Write the failing test** — add `"open_speaker_ui"` to `EXPECTED_TOOLS` in `tests/test_mcp_server.py` (the tools-list test then fails until the tool exists).

- [ ] **Step 2: Run** → FAIL (tool missing).

- [ ] **Step 3: Implement** — in `mcp_server.py`:

```python
import subprocess


def _open_url(url: str) -> None:
    subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        ["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
```

and the tool:

```python
    @server.tool(
        description="Open the local speaker Web UI for a meeting (naming wizard)."
    )
    async def open_speaker_ui(meeting_id: int) -> types.CallToolResult:
        return await _dispatch(proxy.open_speaker_ui, meeting_id=meeting_id)
```

Implement `ServiceClient.open_speaker_ui(meeting_id)` in `service_client.py`:

```python
    def open_speaker_ui(self, meeting_id: int) -> Any:
        detail = self._request("GET", f"/meetings/{meeting_id}")
        import subprocess

        url = detail.get("ui_url") or self.cfg.base_url + "/"
        subprocess.Popen(
            ["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return {"opened": url}
```

> Keep the OS launch inside the MCP proxy (server side), not in tests. Tests assert the tool is advertised (tools/list) and that `open_speaker_ui` exists; do not actually spawn a browser in tests.

- [ ] **Step 4:** Update `docs/USAGE.md` manifest (add `"open_speaker_ui"`) and prose table row; bump "22 tools" → "23 tools".

- [ ] **Step 5: Run** → `uv run pytest tests/test_mcp_server.py tests/test_guide.py -q` → PASS.

- [ ] **Step 6: Checkpoint.**

---

### Task 7 (optional): SSE `GET /events`

**Files:**
- Create: `event_bus.py`; modify `app.py`; `api_routes.py` (publish on batch resolve / add endpoint); `api_batch_routes.py`.
- Test: `tests/test_api_batches.py`

**Interfaces:**
- Produces: `GET /events` (SSE, `require_read_auth`) emitting `{"type":"speaker_batch","batch_id":N,"state":"resolved"}` when a batch resolves.

- [ ] **Step 1:** Minimal design — a process-global `EventBus` with `subscribe()`/`publish()`; the batch resolve operation publishes; the endpoint returns `StreamingResponse` of `data: {json}\n\n`. The agent should poll `GET /speaker-batches/{id}` as the guaranteed path; SSE is best-effort.
- [ ] **Step 2:** Implement + test that `GET /events` returns `text/event-stream` and that a resolve publishes an event to a subscriber recorded in-process.
- [ ] **Step 3:** If SSE proves flaky in CI, document polling as the supported contract and leave SSE out.

---

## Self-Review

- Spec §4.3 tables → Task 1. §5.1 trigger/threshold + batch creation → Task 2. §5.2 `ui_url`/`handoff` → Task 3. §5.3 `open_speaker_ui` → Task 6. §5.4 events (polling guaranteed, SSE optional) → Task 7. §6.1 audio sample → Task 5. §6.2 resolve (enroll/attach/skip, remember gate) → Task 4. §7.3 voiceprint-remember gate → Task 4 (`label_cluster` makes "remember=false" store no voiceprint).
- Deferred to P2: `enroll_consents` table + retention/quality/export; top-N suggested speakers on batch items (schema field exists, filled `None` for now); the wizard UI itself (Plan 3).
- Placeholder scan: code steps contain real code; the two "use the module's existing fixture" notes tell the implementer to mirror an existing helper (fixtures differ per test module) while the assertions are complete.
- Type consistency: `MeetingResult.batch_id`, `handoff` shape, `batch_items`/`mark_batch_item`/`close_batch` used consistently across Tasks 1–5.

## Execution Handoff

Inline execution with the same protocol as Plan 1 (checkpoints, no commits; Oracle review after the risky task and at the end).
