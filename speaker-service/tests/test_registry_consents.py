"""Consent records for stored voiceprints: enroll_consents lifecycle.

Given: a durable registry with a speaker.
When: consent rows are added, the latest is read, one is revoked, and the
speaker is deleted.
Then: consents round-trip in order, the latest is returned, revocation is
stamped, and deleting the speaker cascades its consents away.
"""

from __future__ import annotations

from pathlib import Path

from registry import open_registry


def test_consent_records_roundtrip_latest_and_revoke(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("张三")
        first = registry.add_consent(
            speaker_id, purpose="enrollment", retention_until="2027-10-02T00:00:00Z"
        )
        registry.add_consent(
            speaker_id, purpose="re-enroll", retention_until="2028-10-02T00:00:00Z"
        )
        # Then: consents round-trip in insertion order, latest is the newest
        consents = registry.consents_for_speaker(speaker_id)
        assert [c.purpose for c in consents] == ["enrollment", "re-enroll"]
        latest = registry.latest_consent_for_speaker(speaker_id)
        assert latest is not None and latest.purpose == "re-enroll"
        assert (latest.granted_at, latest.revoked_at) != ("", None)
        # When: one consent is revoked
        assert registry.revoke_consent(first) is True
        assert registry.consents_for_speaker(speaker_id)[0].revoked_at is not None


def test_consents_cascade_with_speaker_delete(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        speaker_id = registry.add_speaker("李四")
        registry.add_consent(
            speaker_id, purpose="enrollment", retention_until="2027-10-02T00:00:00Z"
        )
        # When: the speaker is deleted
        registry.delete_speaker(speaker_id)
        # Then: the consent rows are gone
        assert registry.consents_for_speaker(speaker_id) == []
