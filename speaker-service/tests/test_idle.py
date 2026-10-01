"""Idle-watchdog contract: in-flight request + queued/running job + keepalive.

The clock and sleep seams keep every case deterministic and fast; the live
end-to-end exit path is exercised by the task-29 acceptance log with a real
uvicorn subprocess.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from fastapi.testclient import TestClient

import idle
from config import Settings
from registry import JobState, open_registry


class FakeClock:
    """Deterministic monotonic clock advanced by the fake sleeper."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _advancing_sleep(
    clock: FakeClock,
    *,
    step: float = 1.0,
    hooks: list[Callable[[float], None]] | None = None,
) -> idle.Sleep:
    async def sleep(_interval: float) -> None:
        clock.advance(step)
        for hook in hooks or []:
            hook(clock.now)

    return sleep


def _watchdog(
    tracker: idle.ActivityTracker,
    clock: FakeClock,
    *,
    idle_after_s: float = 5.0,
    jobs_active: Callable[[], bool] | None = None,
    on_idle: Callable[[], None] | None = None,
    hooks: list[Callable[[float], None]] | None = None,
) -> idle.IdleWatchdog:
    return idle.IdleWatchdog(
        tracker=tracker,
        jobs_active=jobs_active if jobs_active is not None else (lambda: False),
        idle_after_s=idle_after_s,
        on_idle=on_idle if on_idle is not None else (lambda: None),
        check_interval_s=1.0,
        clock=clock,
        sleep=_advancing_sleep(clock, hooks=hooks),
    )


def _run(watchdog: idle.IdleWatchdog) -> None:
    asyncio.run(asyncio.wait_for(watchdog.run(), timeout=5.0))


def test_tracker_counts_in_flight_and_records_only_health_keepalives() -> None:
    # Given: a tracker on a frozen clock
    clock = FakeClock()
    tracker = idle.ActivityTracker(clock=clock)

    # When: two requests start, a non-health one finishes, time passes,
    # then a health request finishes
    tracker.request_started()
    tracker.request_started()
    clock.advance(30.0)
    tracker.request_finished("/meetings")
    clock.advance(5.0)
    tracker.request_finished("/health")

    # Then: only /health moved the keepalive; both requests are accounted
    assert tracker.in_flight == 0
    assert tracker.last_keepalive_at == 1035.0


def test_idle_for_is_none_while_a_request_is_in_flight() -> None:
    # Given: an in-flight request
    clock = FakeClock()
    tracker = idle.ActivityTracker(clock=clock)
    tracker.request_started()
    watchdog = _watchdog(tracker, clock)

    # When: the clock moves far past the window
    clock.advance(600.0)

    # Then: the service is busy, and the window restarts from that poll
    assert watchdog.idle_for(clock.now) is None
    tracker.request_finished("/meetings")
    assert watchdog.idle_for(clock.now) == 0.0


def test_idle_for_is_none_while_a_job_is_queued_or_running() -> None:
    # Given: a job the registry reports as active
    clock = FakeClock()
    tracker = idle.ActivityTracker(clock=clock)
    active = True
    watchdog = _watchdog(tracker, clock, jobs_active=lambda: active)

    # When: the clock moves past the window while the job runs
    clock.advance(600.0)

    # Then: the watchdog treats the service as busy until the job is terminal
    assert watchdog.idle_for(clock.now) is None
    active = False
    assert watchdog.idle_for(clock.now) == 0.0


def test_watchdog_fires_once_after_the_full_idle_window() -> None:
    # Given: no requests, no jobs, nothing else touching the service
    clock = FakeClock()
    tracker = idle.ActivityTracker(clock=clock)
    calls: list[float] = []
    watchdog = _watchdog(tracker, clock, on_idle=lambda: calls.append(clock.now))

    # When: the watchdog loop runs with a 1 s poll and a 5 s window
    _run(watchdog)

    # Then: it requested shutdown exactly once, after 5 idle seconds
    assert calls == [1005.0]


def test_watchdog_waits_the_window_after_the_last_busy_poll() -> None:
    # Given: a job that is active for the first 9 polls and then finishes
    clock = FakeClock()
    tracker = idle.ActivityTracker(clock=clock)
    calls: list[float] = []
    watchdog = _watchdog(
        tracker,
        clock,
        jobs_active=lambda: clock.now < 1010.0,
        on_idle=lambda: calls.append(clock.now),
    )

    # When: the loop runs
    _run(watchdog)

    # Then: the window starts at the LAST busy poll (1009) + 5 s
    assert calls == [1014.0]


def test_health_keepalive_resets_the_idle_window() -> None:
    # Given: an open UI sending a /health keepalive 2 s before the window ends
    clock = FakeClock()
    tracker = idle.ActivityTracker(clock=clock)
    calls: list[float] = []

    def keepalive(now: float) -> None:
        if now == 1003.0:
            tracker.request_started()
            tracker.request_finished("/health")

    watchdog = _watchdog(
        tracker,
        clock,
        on_idle=lambda: calls.append(clock.now),
        hooks=[keepalive],
    )

    # When: the loop runs
    _run(watchdog)

    # Then: without the keepalive it would have fired at 1005; it fires at
    # 1003 + 5 instead
    assert calls == [1008.0]


def test_zero_window_never_fires() -> None:
    # Given: the always-on opt-in config (VASTACK_IDLE_EXIT_S=0)
    clock = FakeClock()
    tracker = idle.ActivityTracker(clock=clock)
    calls: list[float] = []
    watchdog = _watchdog(
        tracker, clock, idle_after_s=0.0, on_idle=lambda: calls.append(clock.now)
    )

    # When: the loop runs
    _run(watchdog)

    # Then: it returns immediately without requesting shutdown
    assert calls == []


def test_registry_reports_queued_or_running_jobs(db_path) -> None:
    # Given: a fresh registry with one meeting
    with open_registry(db_path) as registry:
        meeting_id = registry.create_meeting("idle-registry-test")

        # When/Then: the flag follows the job state machine exactly
        assert registry.has_active_jobs() is False
        job_id = registry.create_job(meeting_id)
        assert registry.has_active_jobs() is True
        registry.update_job(job_id, JobState.RUNNING)
        assert registry.has_active_jobs() is True
        registry.update_job(job_id, JobState.DONE)
        assert registry.has_active_jobs() is False
        registry.update_job(job_id, JobState.FAILED, "boom")
        assert registry.has_active_jobs() is False


def test_app_wires_the_watchdog_window_and_can_be_disabled(make_app, tmp_path) -> None:
    # Given/When: three app builds with different idle settings
    enabled = make_app(settings=Settings(data_dir=tmp_path, token=None, idle_exit_s=30))
    zero = make_app(settings=Settings(data_dir=tmp_path, token=None, idle_exit_s=0))
    off = make_app(enable_idle_watchdog=False)

    # Then: only the positive window builds a watchdog
    assert enabled.state.idle_watchdog is not None
    assert enabled.state.idle_watchdog.idle_after_s == 30.0
    assert zero.state.idle_watchdog is None
    assert off.state.idle_watchdog is None


def test_health_keepalive_updates_the_tracker_through_the_middleware(make_app) -> None:
    # Given: a running app and its activity tracker
    app = make_app()
    client = TestClient(app)
    tracker = app.state.activity
    before = tracker.last_keepalive_at
    time.sleep(0.005)

    # When: the UI keepalive is polled
    assert client.get("/health").status_code == 200

    # Then: the keepalive moved; the in-flight count is balanced again
    assert tracker.last_keepalive_at > before
    assert tracker.in_flight == 0

    # And: a non-health request leaves the keepalive alone
    keepalive = tracker.last_keepalive_at
    assert client.get("/missing-route").status_code == 404
    assert tracker.last_keepalive_at == keepalive
