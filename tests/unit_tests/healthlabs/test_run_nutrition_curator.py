from __future__ import annotations

import asyncio
import signal
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from mealie.services.healthlabs import nutrition_curator_runner as runner
from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorReport


class StopEvent:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[int] = []

    def is_set(self) -> bool:
        return self.stopped

    def set(self, *_args) -> None:
        self.stopped = True

    def wait(self, seconds: int) -> bool:
        self.waits.append(seconds)
        self.stopped = True
        return True


def _settings():
    return SimpleNamespace(
        HEALTHLABS_NUTRITION_CURATOR_ENABLED=True,
        HEALTHLABS_NUTRITION_CURATOR_POLL_SECONDS=30,
        HEALTHLABS_NUTRITION_CURATOR_CONCURRENCY=2,
        HEALTHLABS_NUTRITION_CURATOR_MAX_CONCURRENCY=4,
    )


def test_main_waits_interruptibly_when_idle(monkeypatch) -> None:
    stop = StopEvent()
    handlers = []

    @contextmanager
    def session_context():
        yield object()

    class Worker:
        def __init__(self, session, *, should_stop):
            del session
            assert should_stop == stop.is_set

        def work_ids(self):
            return []

    monkeypatch.setattr(runner, "STOP", stop)
    monkeypatch.setattr(runner, "get_app_settings", _settings)
    monkeypatch.setattr(runner, "session_context", session_context)
    monkeypatch.setattr(runner, "NutritionCuratorWorker", Worker)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.append((signum, handler)))

    assert runner.main() == 0
    assert stop.waits == [30]
    assert [signum for signum, _ in handlers][-2:] == [signal.SIGTERM, signal.SIGINT]


def test_signal_during_work_allows_current_decision_to_finish(monkeypatch, caplog) -> None:
    caplog.set_level("INFO")
    stop = StopEvent()
    calls = 0

    @contextmanager
    def session_context():
        yield object()

    class Worker:
        def __init__(self, session, *, should_stop):
            del session, should_stop

        def work_ids(self):
            return [uuid4()]

        async def run_work(self, _work_id):
            nonlocal calls
            calls += 1
            stop.set()
            return NutritionCuratorReport(1, 1, 0, 0, False)

    monkeypatch.setattr(runner, "STOP", stop)
    monkeypatch.setattr(runner, "get_app_settings", _settings)
    monkeypatch.setattr(runner, "session_context", session_context)
    monkeypatch.setattr(runner, "NutritionCuratorWorker", Worker)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    assert runner.main() == 0
    assert calls == 1
    assert stop.waits == []
    assert "approved=1" in caplog.text
    assert "provider-secret" not in caplog.text


def test_drain_uses_distinct_sessions_and_increases_concurrency(monkeypatch) -> None:
    stop = StopEvent()
    sessions: list[object] = []
    active = 0
    peak = 0

    @contextmanager
    def session_context():
        session = object()
        sessions.append(session)
        yield session

    class Worker:
        def __init__(self, session, *, should_stop):
            self.session = session
            assert should_stop == stop.is_set

        async def run_work(self, _work_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return NutritionCuratorReport(1, 1, 0, 0, False)

    monkeypatch.setattr(runner, "STOP", stop)
    monkeypatch.setattr(runner, "session_context", session_context)
    monkeypatch.setattr(runner, "NutritionCuratorWorker", Worker)

    report, concurrency, frozen = asyncio.run(
        runner._drain(
            [uuid4() for _ in range(20)],
            initial_concurrency=2,
            max_concurrency=4,
        )
    )

    assert (report.claimed, report.approved) == (20, 20)
    assert (concurrency, peak, frozen) == (4, 4, False)
    assert len(sessions) == len({id(session) for session in sessions}) == 20


def test_drain_holds_concurrency_after_first_throttle(monkeypatch) -> None:
    stop = StopEvent()
    throttled_id = uuid4()
    active = 0
    peak = 0

    @contextmanager
    def session_context():
        yield object()

    class Worker:
        def __init__(self, session, *, should_stop):
            del session
            assert should_stop == stop.is_set

        async def run_work(self, work_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            if work_id == throttled_id:
                return NutritionCuratorReport(1, 0, 0, 0, False, provider_error=True, throttled=True)
            return NutritionCuratorReport(1, 1, 0, 0, False)

    monkeypatch.setattr(runner, "STOP", stop)
    monkeypatch.setattr(runner, "session_context", session_context)
    monkeypatch.setattr(runner, "NutritionCuratorWorker", Worker)

    report, concurrency, frozen = asyncio.run(
        runner._drain(
            [throttled_id, *(uuid4() for _ in range(19))],
            initial_concurrency=2,
            max_concurrency=8,
        )
    )

    assert report.throttled is True
    assert (concurrency, peak, frozen) == (2, 2, True)
