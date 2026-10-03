# VoiceStack P2a · Voice-library governance & consent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every stored voiceprint traceable to an explicit consent record with a retention term; expose retention/consent in the speaker directory; add export and re-enroll; and surface it in the vanilla UI.

**Architecture:** Schema v4 adds `enroll_consents`. All voiceprint-storing HTTP paths (`POST /speakers/enroll`, `POST /speakers/{id}/voiceprints`, batch resolve with `remember:true`) write a consent row. New read/export/re-enroll routes + UI controls. No new runtime deps.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite + sqlite-vec, pytest, vanilla JS UI.

## Global Constraints

- Work dir: `/Users/LinRui/voicestack/speaker-service`; tests via `uv run pytest ...`.
- **Do not commit** unless the user explicitly approves.
- Additive-only schema changes; existing DBs open and keep data.
- New dataclass fields get defaults.
- **Consent is recorded whenever a voiceprint is stored**; the "remember=false" wizard path stores no voiceprint and thus no consent row.
- Never log or expose the service token; export is same-origin cookie-auth read.
- Preserve all existing `data-testid`s (nav count stays 5).

## Backend contracts (existing)

- `POST /speakers/enroll` (body name/organization/title/notes/cluster_id) → creates speaker + voiceprint
- `POST /speakers/{id}/voiceprints` (body cluster_id/meeting_id?) → adds a voiceprint to an existing speaker
- `POST /speaker-batches/{id}/resolve` → enroll(remember) / attach(remember) / skip
- `GET /speakers` → speakers incl. `title`, `voiceprint_count`
- `DELETE /speakers/{id}` → purges voiceprints + unlinks

---

### Task 1: Schema v4 — `enroll_consents` + consent registry ops

**Files:** `registry_schema.py`, `registry_models.py`, `registry_support.py`, `registry_consents.py` (new), `registry.py`; Test: `tests/test_registry_consents.py`, `tests/test_registry_migrations.py`

**Interfaces:**
- Produces:
  - `SCHEMA_VERSION == 4`; table `enroll_consents(id, speaker_id, granted_at, purpose, retention_until, source_batch_id, revoked_at)`
  - `ConsentOps.add_consent(speaker_id, *, purpose, retention_until, source_batch_id=None) -> int`
  - `ConsentOps.consents_for_speaker(speaker_id) -> list[SpeakerConsent]`
  - `ConsentOps.latest_consent_for_speaker(speaker_id) -> SpeakerConsent | None`
  - `ConsentOps.revoke_consent(consent_id) -> bool`

- [ ] **Step 1: Failing test** — `tests/test_registry_consents.py`:

```python
from __future__ import annotations

from pathlib import Path

from registry import open_registry


def test_consent_records_roundtrip_and_latest(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("张三")
        first = registry.add_consent(
            speaker_id, purpose="enrollment", retention_until="2027-10-02T00:00:00Z"
        )
        registry.add_consent(
            speaker_id,
            purpose="re-enroll",
            retention_until="2028-10-02T00:00:00Z",
            source_batch_id=None,
        )
        consents = registry.consents_for_speaker(speaker_id)
        assert [c.purpose for c in consents] == ["enrollment", "re-enroll"]
        assert registry.latest_consent_for_speaker(speaker_id).purpose == "re-enroll"
        assert registry.revoke_consent(first) is True
        assert registry.consents_for_speaker(speaker_id)[0].revoked_at is not None


def test_consents_cascade_with_speaker_delete(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("李四")
        registry.add_consent(
            speaker_id, purpose="enrollment", retention_until="2027-10-02T00:00:00Z"
        )
        registry.delete_speaker(speaker_id)
        assert registry.consents_for_speaker(speaker_id) == []
```

- [ ] **Step 2: Run** → FAIL (`cannot import name` / no attribute).

- [ ] **Step 3: Implement.** `registry_schema.py`: `SCHEMA_VERSION = 4`; add

```python
_SCHEMA_V4: Final = """
CREATE TABLE IF NOT EXISTS enroll_consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    purpose TEXT NOT NULL,
    retention_until TEXT NOT NULL,
    source_batch_id INTEGER REFERENCES speaker_batches(id) ON DELETE SET NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_enroll_consents_speaker
    ON enroll_consents(speaker_id);
"""
```

add `_add_consent_table(conn)` (executescript), call it from `_apply_schema`, and in `_migrate` add `if from_version < 4: _add_consent_table(conn)` before the stamp.

`registry_models.py`: add

```python
@dataclass(frozen=True, slots=True)
class SpeakerConsent:
    id: int
    speaker_id: int
    granted_at: str
    purpose: str
    retention_until: str
    source_batch_id: int | None
    revoked_at: str | None
```

`registry_support.py`: `consent_from_row(row) -> SpeakerConsent`.

`registry_consents.py` (new): `ConsentOps` with the four methods (`add_consent` raises `SpeakerNotFoundError` when the speaker is missing; `latest_consent_for_speaker` orders by id DESC LIMIT 1 and skips revoked? No — latest regardless, the caller decides). `registry.py`: mix in `ConsentOps`, export `SpeakerConsent`.

Update `tests/test_registry_migrations.py`: replace any `== SCHEMA_VERSION` (already dynamic) and add a v3→v4 assertion inside the existing v2→v3 test? Add a new test that a v3-stamped DB gains `enroll_consents`.

- [ ] **Step 4: Run** → `uv run pytest tests/test_registry_consents.py tests/test_registry_migrations.py -q` → PASS.

- [ ] **Step 5: Checkpoint (commit only if approved).**

---

### Task 2: Record consent on every voiceprint store + expose retention

**Files:** `config.py`, `api_operations.py`, `api_service.py`; Test: `tests/test_api_speakers.py`, `tests/test_api_batches.py`

**Interfaces:**
- Produces: `Settings.consent_retention_days: int = 365`; `speaker_payload` gains `consent_granted_at`, `retention_until`, `consent_expired: bool`.

- [ ] **Step 1: Failing test** — add to `tests/test_api_speakers.py`:

```python
def test_enroll_records_consent_and_exposes_retention(
    client: TestClient, auth: dict[str, str], upload_meeting, db_path
) -> None:
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    created = client.post(
        "/speakers/enroll", headers=auth,
        json={"name": "王五", "cluster_id": cluster_id},
    )
    assert created.status_code == 201
    speaker = client.get("/speakers", headers=auth).json()["speakers"][0]
    assert speaker["consent_granted_at"]
    assert speaker["retention_until"]
    assert speaker["consent_expired"] is False
    with open_registry(db_path) as registry:
        consents = registry.consents_for_speaker(speaker["id"])
        assert len(consents) == 1 and consents[0].purpose == "enrollment"
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.**
  - `config.py` `Settings`: `consent_retention_days: int = 365`.
  - `api_operations.py`: add a helper

```python
def _record_consent(registry: Registry, speaker_id: int, *, purpose: str, source_batch_id: int | None = None) -> None:
    from datetime import UTC, datetime, timedelta

    days = get_settings().consent_retention_days
    until = (datetime.now(UTC) + timedelta(days=days)).isoformat()
    registry.add_consent(speaker_id, purpose=purpose, retention_until=until, source_batch_id=source_batch_id)
```

  call it in `enroll_new_speaker` (purpose `"enrollment"`), `attach_voiceprint` (purpose `"attach"`), and in `resolve_batch` after the `remember` enroll/attach (pass `source_batch_id=batch_id`). Import `get_settings` from `config`.
  - `api_service.speaker_payload(speaker, voiceprint_count, *, consent=None)` → add `consent_granted_at`, `retention_until`, `consent_expired` (compare to now). Update callers (`api_speaker_routes.list_speakers`, `create_speaker`, `update_speaker`) to pass the latest consent.

- [ ] **Step 4: Run** → `uv run pytest tests/test_api_speakers.py tests/test_api_batches.py -q` → PASS.

- [ ] **Step 5: Checkpoint.**

---

### Task 3: Export a speaker (data portability)

**Files:** `api_operations.py`, `api_speaker_routes.py`; Test: `tests/test_api_speakers.py`

**Interfaces:**
- Produces: `GET /speakers/{id}/export` → `{speaker, consents:[...], voiceprints:[{id,model_id,revision,dim,created_at,vector_base64}]}`; 404 if missing.

- [ ] **Step 1: Failing test**:

```python
def test_export_speaker_returns_metadata_consents_and_vectors(
    client: TestClient, auth: dict[str, str], upload_meeting
) -> None:
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    sp = client.post("/speakers/enroll", headers=auth, json={"name": "赵六", "cluster_id": cluster_id}).json()
    exported = client.get(f"/speakers/{sp['speaker_id']}/export", headers=auth)
    assert exported.status_code == 200
    body = exported.json()
    assert body["speaker"]["name"] == "赵六"
    assert len(body["consents"]) == 1
    assert len(body["voiceprints"]) == 1
    assert body["voiceprints"][0]["vector_base64"]
    assert client.get("/speakers/999/export", headers=auth).status_code == 404
```

- [ ] **Step 2: Run** → FAIL (404).

- [ ] **Step 3: Implement** `api_operations.export_speaker(registry, speaker_id) -> dict`:
  - 404 if speaker missing.
  - `base64.b64encode(vp.vector.astype("<f4").tobytes()).decode()` per voiceprint.
  - include consents via `consents_for_speaker`.
  - route `GET /speakers/{id}/export` (`require_read_auth`).

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Checkpoint.**

---

### Task 4: Re-enroll a speaker (resolve `re_enroll_required`)

**Files:** `registry_speakers.py`, `api_operations.py`, `api_schemas.py`, `api_speaker_routes.py`; Test: `tests/test_api_speakers.py`

**Interfaces:**
- Produces:
  - `SpeakerOps.delete_stale_voiceprints(speaker_id, model_id, revision) -> int`
  - `POST /speakers/{id}/re-enroll` body `{cluster_id}` → attaches a fresh voiceprint with the running model, purges stale ones, records consent; 404 unknown speaker/cluster.

- [ ] **Step 1: Failing test**:

```python
def test_re_enroll_replaces_stale_voiceprint_and_records_consent(
    client: TestClient, auth: dict[str, str], upload_meeting, db_path
) -> None:
    meeting = upload_meeting(client)
    cluster_id = meeting["unknown_clusters"][0]["cluster_id"]
    sp = client.post("/speakers/enroll", headers=auth, json={"name": "钱七", "cluster_id": cluster_id}).json()
    other = upload_meeting(client, title="Second")
    again = client.post(
        f"/speakers/{sp['speaker_id']}/re-enroll", headers=auth,
        json={"cluster_id": other["unknown_clusters"][0]["cluster_id"]},
    )
    assert again.status_code == 200
    assert again.json()["voiceprint_id"] > 0
    with open_registry(db_path) as registry:
        assert len(registry.voiceprints_for_speaker(sp["speaker_id"])) == 1
        purposes = [c.purpose for c in registry.consents_for_speaker(sp["speaker_id"])]
        assert purposes == ["enrollment", "re-enroll"]
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.**
  - `registry_speakers.SpeakerOps.delete_stale_voiceprints(speaker_id, model_id, revision)`:
    delete voiceprints for the speaker whose `model_id != ? OR revision != ?` (vec trigger purges the index).
  - `api_schemas.ReEnrollRequest(_Strict)` `{cluster_id: int}`.
  - `api_operations.re_enroll(registry, deps, speaker_id, cluster_id)`:
    resolve meeting; `attach_voiceprint(...)` (fresh voiceprint); `delete_stale_voiceprints(speaker_id, MODEL_ID, MODEL_REVISION)`; `_record_consent(..., purpose="re-enroll")`; return `{"speaker_id", "voiceprint_id", "cluster_id"}`.
  - route `POST /speakers/{id}/re-enroll` (`require_mutation_auth`).

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Checkpoint.**

---

### Task 5: UI — retention/consent badges + export/re-enroll

**Files:** `ui_static/index.html`, `ui_static/app.js`, `ui_static/app.css`; Test: `tests/test_ui.py`, `tests/e2e/ui_e2e.mjs`

**Interfaces:**
- Produces: speaker directory rows show 职务 + 同意日期 + 保留期 and a `保留期已到` badge when `consent_expired`; a 导出 button (`data-export="{id}"`) downloads the export JSON; a 重新登记 button (`data-reenroll="{id}"`) opens a small prompt to choose a cluster (or uses the currently selected transcript cluster) and POSTs re-enroll.

- [ ] **Step 1:** `renderSpeakers()`: add cells/testids `speaker-consent-{id}`, `speaker-retention-{id}`; buttons `data-export`, `data-reenroll`.
- [ ] **Step 2:** Export handler → `fetch('/speakers/{id}/export', {credentials:'same-origin'})` → download a Blob as `speaker-{id}.json`; status in `#speaker-status`.
- [ ] **Step 3:** Re-enroll handler → if a transcript cluster is selected, confirm and POST; else status "先选择要用于重新登记的 cluster"。
- [ ] **Step 4:** `test_ui.py`: assert `speaker-consent-` / `speaker-retention-` / `data-export` / `data-reenroll` appear in `app.js`.
- [ ] **Step 5:** Run `uv run pytest -q` → all green; `node --check ui_static/app.js`.

---

## Self-Review

- Spec §4.4 consent table → Task 1. §7.3 consent/retention + export → Tasks 2, 3. §3.3 `re_enroll_required` resolution → Task 4. UI governance → Task 5.
- Consent recorded whenever a voiceprint is stored; `remember=false` stores none → no consent row (verified by Task 2 test inputs).
- Deferred: voiceprint "quality score" and "last used"; auto-purge on expiry (we only flag `consent_expired`); raw-vector export is included (it is the user's own enrolled data).
- Placeholder scan: code steps are concrete.

## Execution Handoff

Inline execution (same protocol): checkpoints; Oracle review after Task 1 and at the end (retrieve results via `session_read` if `background_output` reports Aborted).
