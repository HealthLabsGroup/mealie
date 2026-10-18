from __future__ import annotations

import signal
from contextlib import contextmanager
from types import SimpleNamespace

from mealie.services.healthlabs import nutrition_curator_runner as runner


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

        def run_once(self):
            return SimpleNamespace(claimed=0, approved=0, insufficient_evidence=0, failed=0)

    monkeypatch.setattr(runner, "STOP", stop)
    monkeypatch.setattr(runner, "get_app_settings", _settings)
    monkeypatch.setattr(runner, "session_context", session_context)
    monkeypatch.setattr(runner, "NutritionCuratorWorker", Worker)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.append((signum, handler)))

    assert runner.main() == 0
    assert stop.waits == [30]
    assert [signum for signum, _ in handlers] == [signal.SIGTERM, signal.SIGINT]


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

        def run_once(self):
            nonlocal calls
            calls += 1
            stop.set()
            return SimpleNamespace(claimed=1, approved=1, insufficient_evidence=0, failed=0)

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
