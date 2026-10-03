"""Batch persistence: speaker_batches + batch_items lifecycle.

Given: a durable registry with a meeting holding two unknown clusters.
When: a speaker batch is created, items added, an item marked and the batch
closed.
Then: the batch and its items round-trip, ``open_batch_for_meeting`` finds only
open batches, and deleting the meeting cascades the batch away.
"""

from __future__ import annotations

from pathlib import Path

from registry import BatchState, open_registry


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
        # Then: the batch and its items round-trip
        batch = registry.get_speaker_batch(batch_id)
        assert batch is not None and batch.meeting_id == meeting_id
        assert batch.state is BatchState.OPEN
        items = registry.batch_items(batch_id)
        assert [item.cluster_id for item in items] == [first, second]
        assert items[0].similarity == 0.42
        # And: only the open batch is found by meeting
        assert registry.open_batch_for_meeting(meeting_id).id == batch_id
        # When: an item is marked and the batch closed
        assert registry.mark_batch_item(batch_id, first, "enrolled") is True
        assert registry.close_batch(batch_id) is True
        # Then: it is no longer open but remains readable
        assert registry.open_batch_for_meeting(meeting_id) is None
        closed = registry.get_speaker_batch(batch_id)
        assert closed is not None and closed.state is BatchState.RESOLVED
        assert closed.resolved_at is not None


def test_batch_items_cascade_when_meeting_deleted(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        meeting_id, first, _ = _meeting(registry)
        batch_id = registry.create_speaker_batch(meeting_id)
        registry.add_batch_item(batch_id, first)
        # When: the meeting is deleted
        assert registry.delete_meeting(meeting_id) is True
        # Then: the batch and its items cascade away
        assert registry.get_speaker_batch(batch_id) is None
        assert registry.batch_items(batch_id) == []


def test_create_speaker_batch_reuses_the_open_batch(db_path: Path) -> None:
    with open_registry(db_path) as registry:
        meeting_id, first, _ = _meeting(registry)
        # Given: an open batch for the meeting
        batch_id = registry.create_speaker_batch(meeting_id)
        registry.add_batch_item(batch_id, first)
        # When/Then: a second create reuses the open batch instead of duplicating
        assert registry.create_speaker_batch(meeting_id) == batch_id
        assert len(registry.batch_items(batch_id)) == 1
