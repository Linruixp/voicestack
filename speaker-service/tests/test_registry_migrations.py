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

from registry import SCHEMA_VERSION, default_db_path, open_registry


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
