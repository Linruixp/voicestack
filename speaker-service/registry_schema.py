"""SQLite file lifecycle for the durable speaker registry.

Owns what concerns the database FILE rather than the domain: opening it
owner-only, loading the sqlite-vec extension, enabling WAL, the DDL (including
the cosine ``voiceprints_vec`` index and its sync triggers), the
``schema_version`` stamp, and the timestamped backup taken before a migration.

Foreign-key ON DELETE policy declared here:
- ``voiceprints`` -> CASCADE (biometric data dies with its speaker),
- ``meeting_speakers.speaker_id`` / ``segments.speaker_id`` -> SET NULL
  (transcript history outlives the speaker),
- clusters are reset to ``unknown`` by ``Registry.delete_speaker`` because SQL
  alone cannot clear a label through the ``meeting_speakers`` link.

Schema versions: v1 is the initial schema.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import sqlite_vec

SCHEMA_VERSION: Final = 1
VOICEPRINT_DIM: Final = 256
BACKUP_DIR_NAME: Final = "backups"

_SCHEMA_V1: Final = """
CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    organization TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS voiceprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    model_id TEXT NOT NULL CHECK (length(model_id) > 0),
    revision TEXT NOT NULL CHECK (length(revision) > 0),
    dim INTEGER NOT NULL CHECK (dim > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_voiceprints_speaker ON voiceprints(speaker_id);

CREATE VIRTUAL TABLE IF NOT EXISTS voiceprints_vec USING vec0(
    voiceprint_id INTEGER PRIMARY KEY,
    embedding FLOAT[256] distance_metric=cosine
);
CREATE TRIGGER IF NOT EXISTS voiceprints_vec_insert AFTER INSERT ON voiceprints BEGIN
    INSERT INTO voiceprints_vec(voiceprint_id, embedding) VALUES (new.id, new.embedding);
END;
CREATE TRIGGER IF NOT EXISTS voiceprints_vec_delete AFTER DELETE ON voiceprints BEGIN
    DELETE FROM voiceprints_vec WHERE voiceprint_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS voiceprints_vec_update
AFTER UPDATE OF embedding ON voiceprints BEGIN
    DELETE FROM voiceprints_vec WHERE voiceprint_id = old.id;
    INSERT INTO voiceprints_vec(voiceprint_id, embedding) VALUES (new.id, new.embedding);
END;

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    date TEXT,
    audio_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    label TEXT,
    state TEXT NOT NULL DEFAULT 'unknown'
        CHECK (state IN ('unknown', 'named', 'attached')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_clusters_meeting ON clusters(meeting_id);

CREATE TABLE IF NOT EXISTS meeting_speakers (
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    speaker_id INTEGER REFERENCES speakers(id) ON DELETE SET NULL,
    cluster_id INTEGER NOT NULL UNIQUE REFERENCES clusters(id) ON DELETE CASCADE,
    confidence REAL,
    PRIMARY KEY (meeting_id, cluster_id)
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    "start" REAL NOT NULL,
    "end" REAL NOT NULL,
    text TEXT NOT NULL,
    speaker_id INTEGER REFERENCES speakers(id) ON DELETE SET NULL,
    cluster_id INTEGER REFERENCES clusters(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON segments(meeting_id);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'done', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_meeting ON jobs(meeting_id);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


class SchemaError(RuntimeError):
    """The registry database file could not be opened or migrated."""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` owner-only with WAL, foreign keys and sqlite-vec.

    A fresh database gets the current schema; an older one is backed up to
    ``<backups>/<stem>-<timestamp>.db`` before migrating.
    """
    _secure_dir(db_path.parent)
    _ensure_file_owner_only(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        _load_vec(conn)
        version = current_version(conn)
        if version is None:
            _apply_schema(conn)
        elif version < SCHEMA_VERSION:
            _backup(conn, db_path)
            _migrate(conn, version)
    except Exception:  # noqa: BROAD_EXCEPT_OK - close the handle, then re-raise
        conn.close()
        raise
    return conn


def current_version(conn: sqlite3.Connection) -> int | None:
    """The newest ``schema_version`` stamp, or ``None`` for an unstamped file."""
    stamped = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if stamped is None:
        return None
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


def _load_vec(conn: sqlite3.Connection) -> None:
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except (AttributeError, sqlite3.OperationalError) as exc:
        raise SchemaError("sqlite-vec extension could not be loaded") from exc
    finally:
        conn.enable_load_extension(False)


def _apply_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(_SCHEMA_V1)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _utc_now()),
        )


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version < 1:
        _apply_schema(conn)


def _backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    """Consistent, timestamped copy of the database, taken before migrating."""
    directory = db_path.parent / BACKUP_DIR_NAME
    _secure_dir(directory)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{db_path.stem}-{stamp}.db"
    destination = sqlite3.connect(target)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    os.chmod(target, 0o700)
    return target


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Enforce owner-only even when the directory pre-existed. Older installs
    # (and macOS' own Application Support root) created the app-data dir 0755;
    # the registry, backups and uploads must never be world-traversable.
    os.chmod(path, 0o700)


def _ensure_file_owner_only(path: Path) -> None:
    """Pre-create the database 700 so SQLite's WAL sidecars inherit that mode."""
    if not path.exists():
        handle = os.open(path, os.O_CREAT | os.O_WRONLY, 0o700)
        os.close(handle)
    os.chmod(path, 0o700)
