"""Migration/backup and default-location behavior of the durable registry.

Given: a legacy database stamped below the current schema version, and the
host's default app-data location.
When: the registry opens the legacy file (migrating it) and resolves the
default database path.
Then: a timestamped owner-only backup of the pre-migration file exists, the
old data is intact in both the backup and the migrated file, and the default
path lives under the owner-only ``VoiceStudioStack`` app-support directory.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from registry import SCHEMA_VERSION, default_db_path, open_registry
from registry_schema import _SCHEMA_V1, _SCHEMA_V3, _V2_COLUMNS, SchemaError  # noqa: PLC2701


def test_open_migrates_legacy_database_with_timestamped_backup(
    tmp_path: Path,
) -> None:
    # Given: a legacy database stamped with schema version 0
    legacy_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(legacy_path)
    legacy.executescript(
        """
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (0, '2026-01-01T00:00:00Z');
        CREATE TABLE legacy_note(note TEXT);
        INSERT INTO legacy_note VALUES ('keep me');
        """
    )
    legacy.commit()
    legacy.close()
    # When: the registry opens it (triggering migration)
    with open_registry(legacy_path) as registry:
        assert registry.schema_version() == SCHEMA_VERSION
    # Then: a timestamped backup of the pre-migration database exists
    backups = sorted((tmp_path / "backups").glob("legacy-*.db"))
    assert len(backups) == 1
    assert (backups[0].stat().st_mode & 0o777) == 0o700
    # And: the backup is the OLD snapshot (version 0, pre-migration data intact)
    snapshot = sqlite3.connect(backups[0])
    assert (
        snapshot.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 0
    )
    assert snapshot.execute("SELECT note FROM legacy_note").fetchone()[0] == "keep me"
    snapshot.close()
    # And: the migrated database kept the pre-existing table and is usable
    with open_registry(legacy_path) as registry:
        speaker_id = registry.add_speaker("Alice")
        assert registry.get_speaker(speaker_id) is not None


def test_default_db_path_lives_under_app_support() -> None:
    # Given: no explicit path
    # When: the default database path is resolved
    path = default_db_path()
    # Then: it is the app-data location, not the repo or world-readable /tmp
    assert path == (
        Path.home()
        / "Library"
        / "Application Support"
        / "VoiceStudioStack"
        / "voicestack.db"
    )


def test_open_migrates_v1_database_to_v2_preserving_rows(tmp_path: Path) -> None:
    # Given: a database stamped at v1 (pre-v2) with one speaker row
    v1_path = tmp_path / "v1.db"
    legacy = sqlite3.connect(v1_path)
    legacy.enable_load_extension(True)
    sqlite_vec.load(legacy)
    legacy.enable_load_extension(False)
    legacy.executescript(_SCHEMA_V1)
    legacy.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')"
    )
    legacy.execute("INSERT INTO speakers(name) VALUES ('Alice')")
    legacy.commit()
    legacy.close()
    # When: the registry opens it (triggering the v1 -> v2 migration)
    with open_registry(v1_path) as registry:
        assert registry.schema_version() == SCHEMA_VERSION
        # Then: pre-existing rows survive
        alice = registry.list_speakers()[0]
        assert alice.name == "Alice"
    # And: the nullable v2 columns exist on disk (rows need no backfill)
    check = sqlite3.connect(v1_path)
    meeting_cols = {row[1] for row in check.execute("PRAGMA table_info(meetings)")}
    speaker_cols = {row[1] for row in check.execute("PRAGMA table_info(speakers)")}
    check.close()
    assert {
        "topic",
        "location",
        "duration_s",
        "summary_json",
        "original_title",
    } <= meeting_cols
    assert "title" in speaker_cols


def test_migration_self_heals_when_a_v2_column_already_exists(tmp_path: Path) -> None:
    # Given: a v1 DB where one v2 column was already applied by a failed attempt
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(_SCHEMA_V1)
    conn.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')"
    )
    conn.execute("ALTER TABLE meetings ADD COLUMN topic TEXT")
    conn.commit()
    conn.close()
    # When: the registry opens it
    with open_registry(path) as registry:
        # Then: migration completes instead of failing on the duplicate column
        assert registry.schema_version() == SCHEMA_VERSION
    check = sqlite3.connect(path)
    cols = {row[1] for row in check.execute("PRAGMA table_info(meetings)")}
    check.close()
    assert {"topic", "original_title", "duration_s"} <= cols


def test_unstamped_nonempty_database_is_backed_up_before_apply(tmp_path: Path) -> None:
    # Given: a DB with user data but no schema_version table (pre-versioning)
    path = tmp_path / "unstamped.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE legacy_note(note TEXT)")
    conn.execute("INSERT INTO legacy_note VALUES ('keep me')")
    conn.commit()
    conn.close()
    # When: the registry opens it
    with open_registry(path) as registry:
        assert registry.schema_version() == SCHEMA_VERSION
    # Then: a snapshot was taken and the old data survived
    backups = list((tmp_path / "backups").glob("unstamped-*.db"))
    assert len(backups) == 1
    snapshot = sqlite3.connect(backups[0])
    assert snapshot.execute("SELECT note FROM legacy_note").fetchone()[0] == "keep me"
    snapshot.close()


def test_open_migrates_v2_database_to_v3_adding_batch_tables(tmp_path: Path) -> None:
    # Given: a v2-stamped database (v2 columns present, no batch tables)
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(_SCHEMA_V1)
    for table, column, decl in _V2_COLUMNS:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (2, '2026-01-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO meetings(title) VALUES ('Legacy')")
    conn.commit()
    conn.close()
    # When: the registry opens it
    with open_registry(path) as registry:
        # Then: batch tables are added and old data survives
        assert registry.schema_version() == SCHEMA_VERSION
        meeting_id = registry.list_meetings()[0].id
        batch_id = registry.create_speaker_batch(meeting_id)
        assert registry.get_speaker_batch(batch_id) is not None


def test_open_migrates_v3_database_adding_consent_table(tmp_path: Path) -> None:
    # Given: a v3-stamped database (v2 columns + batch tables, no consents)
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(_SCHEMA_V1)
    for table, column, decl in _V2_COLUMNS:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.executescript(_SCHEMA_V3)
    conn.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (3, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    # When: the registry opens it
    with open_registry(path) as registry:
        # Then: the consent table is added and usable
        assert registry.schema_version() == SCHEMA_VERSION
        speaker_id = registry.add_speaker("Old")
        registry.add_consent(
            speaker_id, purpose="enrollment", retention_until="2027-01-01T00:00:00Z"
        )
        assert registry.latest_consent_for_speaker(speaker_id) is not None


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    # Given: a DB stamped with a future schema version
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO schema_version VALUES (99, '2030-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    # When/Then: opening refuses instead of silently proceeding
    with pytest.raises(SchemaError):
        open_registry(path)
