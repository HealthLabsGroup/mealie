import asyncio
import importlib
from contextlib import contextmanager
from decimal import Decimal
from threading import Event, Thread
from types import SimpleNamespace

from mealie import app
from mealie.services.scheduler import SchedulerRegistry, tasks

task_module = importlib.import_module("mealie.services.scheduler.tasks.reconcile_healthlabs_nutrition")


def _settings(request_limit: int):
    return SimpleNamespace(
        HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT=request_limit,
        HEALTHLABS_NUTRITION_PROVIDER_DAILY_BUDGET_USD=Decimal("1"),
        HEALTHLABS_NUTRITION_PROVIDER_INPUT_USD_PER_MILLION_TOKENS=Decimal("1"),
        HEALTHLABS_NUTRITION_PROVIDER_OUTPUT_USD_PER_MILLION_TOKENS=Decimal("1"),
        HEALTHLABS_NUTRITION_PROVIDER_MAX_OUTPUT_TOKENS=256,
    )


def test_task_drains_worker_until_idle_with_deployment_settings(monkeypatch):
    session = object()
    calls = []
    reports = iter(
        (
            SimpleNamespace(discovered=20, processed=20, completed=10, review_required=10, failed=0),
            SimpleNamespace(discovered=0, processed=0, completed=0, review_required=0, failed=0),
        )
    )

    @contextmanager
    def session_context():
        yield session

    class Worker:
        def __init__(self, worker_session, **kwargs):
            calls.append((worker_session, kwargs))

        async def run(self):
            return next(reports)

    monkeypatch.setattr(task_module, "session_context", session_context)
    monkeypatch.setattr(task_module, "get_app_settings", lambda: _settings(7))
    monkeypatch.setattr(task_module, "NutritionReconciliationWorker", Worker)

    task_module.reconcile_healthlabs_nutrition()

    assert calls == [
        (
            session,
            {
                "batch_size": 20,
                "provider_daily_request_limit": 7,
                "provider_daily_budget_usd": Decimal("1"),
                "provider_input_usd_per_million_tokens": Decimal("1"),
                "provider_output_usd_per_million_tokens": Decimal("1"),
                "provider_max_output_tokens": 256,
            },
        ),
        (
            session,
            {
                "batch_size": 20,
                "provider_daily_request_limit": 7,
                "provider_daily_budget_usd": Decimal("1"),
                "provider_input_usd_per_million_tokens": Decimal("1"),
                "provider_output_usd_per_million_tokens": Decimal("1"),
                "provider_max_output_tokens": 256,
            },
        ),
    ]


def test_task_stops_draining_at_the_wall_time_limit(monkeypatch):
    session = object()
    calls = 0
    clock_reads = 0

    @contextmanager
    def session_context():
        yield session

    class Worker:
        def __init__(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1

        async def run(self):
            return SimpleNamespace(discovered=20, processed=20, completed=20, review_required=0, failed=0)

    def monotonic():
        nonlocal clock_reads
        clock_reads += 1
        return 0 if clock_reads == 1 else 241

    monkeypatch.setattr(task_module, "session_context", session_context)
    monkeypatch.setattr(task_module, "monotonic", monotonic, raising=False)
    monkeypatch.setattr(task_module, "get_app_settings", lambda: _settings(0))
    monkeypatch.setattr(task_module, "NutritionReconciliationWorker", Worker)

    task_module.reconcile_healthlabs_nutrition()

    assert calls == 1
    assert clock_reads == 2


def test_task_contains_worker_errors(monkeypatch):
    @contextmanager
    def session_context():
        yield object()

    class Worker:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            raise RuntimeError("provider response must not escape")

    monkeypatch.setattr(task_module, "session_context", session_context)
    monkeypatch.setattr(task_module, "get_app_settings", lambda: _settings(0))
    monkeypatch.setattr(task_module, "NutritionReconciliationWorker", Worker)

    task_module.reconcile_healthlabs_nutrition()


def test_task_skips_overlapping_invocation(monkeypatch):
    started = Event()
    release = Event()
    calls = 0

    @contextmanager
    def session_context():
        yield object()

    class Worker:
        def __init__(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1

        async def run(self):
            started.set()
            await asyncio.to_thread(release.wait)
            return SimpleNamespace(discovered=0, processed=0, completed=0, review_required=0, failed=0)

    monkeypatch.setattr(task_module, "session_context", session_context)
    monkeypatch.setattr(task_module, "get_app_settings", lambda: _settings(0))
    monkeypatch.setattr(task_module, "NutritionReconciliationWorker", Worker)

    thread = Thread(target=task_module.reconcile_healthlabs_nutrition)
    thread.start()
    assert started.wait(timeout=2)
    task_module.reconcile_healthlabs_nutrition()
    release.set()
    thread.join(timeout=2)

    assert calls == 1
    assert not thread.is_alive()


def test_task_is_registered_minutely(monkeypatch):
    daily = SchedulerRegistry._daily.copy()
    hourly = SchedulerRegistry._hourly.copy()
    minutely = SchedulerRegistry._minutely.copy()

    async def start():
        return None

    monkeypatch.setattr(app.SchedulerService, "start", start)
    try:
        asyncio.run(app.start_scheduler())
        assert tasks.reconcile_healthlabs_nutrition in SchedulerRegistry._minutely
    finally:
        SchedulerRegistry._daily[:] = daily
        SchedulerRegistry._hourly[:] = hourly
        SchedulerRegistry._minutely[:] = minutely
