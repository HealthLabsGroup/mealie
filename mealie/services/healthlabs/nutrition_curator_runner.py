from __future__ import annotations

import asyncio
import signal
from threading import Event
from uuid import UUID

import httpx2

from mealie.core import root_logger
from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorReport, NutritionCuratorWorker
from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataReport, RecipeMetadataWorker

logger = root_logger.get_logger(__name__)
STOP = Event()


def _request_stop(_signum, _frame) -> None:
    STOP.set()


def _combined(first: NutritionCuratorReport, second: NutritionCuratorReport) -> NutritionCuratorReport:
    return NutritionCuratorReport(
        claimed=first.claimed + second.claimed,
        approved=first.approved + second.approved,
        insufficient_evidence=first.insufficient_evidence + second.insufficient_evidence,
        failed=first.failed + second.failed,
        provider_exhausted=first.provider_exhausted or second.provider_exhausted,
        provider_error=first.provider_error or second.provider_error,
        throttled=first.throttled or second.throttled,
        transport_error=first.transport_error or second.transport_error,
    )


async def _run_work(work_id: UUID, http_client: httpx2.AsyncClient | None = None) -> NutritionCuratorReport:
    try:
        with session_context() as session:
            worker = (
                NutritionCuratorWorker(session, should_stop=STOP.is_set)
                if http_client is None
                else NutritionCuratorWorker(session, should_stop=STOP.is_set, http_client=http_client)
            )
            return await worker.run_work(work_id)
    except Exception as exc:
        logger.error("Nutrition curator task failed error_type=%s", type(exc).__name__)
        return NutritionCuratorReport(0, 0, 0, 0, False, provider_error=True)


async def _drain(
    work_ids: list[UUID],
    *,
    initial_concurrency: int,
    max_concurrency: int,
    frozen: bool = False,
    http_client: httpx2.AsyncClient | None = None,
) -> tuple[NutritionCuratorReport, int, bool]:
    total = NutritionCuratorReport(0, 0, 0, 0, False)
    concurrency = min(initial_concurrency, max_concurrency)
    last_stable_concurrency = concurrency
    stable_completions = 0
    transport_errors = 0
    pending: set[asyncio.Task[NutritionCuratorReport]] = set()
    iterator = iter(work_ids)
    exhausted = False

    while pending or not exhausted:
        while len(pending) < concurrency and not exhausted and not STOP.is_set() and not total.provider_exhausted:
            try:
                work_id = next(iterator)
            except StopIteration:
                exhausted = True
                break
            pending.add(asyncio.create_task(_run_work(work_id, http_client)))

        if not pending:
            break

        finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        throttled = False
        for task in finished:
            report = task.result()
            total = _combined(total, report)
            throttled = throttled or report.throttled
            transport_errors += int(report.transport_error)
            if report.claimed and not report.provider_error and not frozen:
                stable_completions += 1

        if throttled and not frozen:
            frozen = True
            logger.warning("Nutrition curator holding concurrency=%s after provider throttling", concurrency)
        transport_error_threshold = max(8, concurrency // 16)
        if not frozen and transport_errors >= transport_error_threshold:
            concurrency = last_stable_concurrency
            frozen = True
            logger.warning("Nutrition curator holding concurrency=%s after transport saturation", concurrency)

        threshold = max(8, concurrency // 4)
        if not frozen and concurrency < max_concurrency and stable_completions >= threshold:
            last_stable_concurrency = concurrency
            next_concurrency = min(max_concurrency, concurrency * 2)
            logger.info("Nutrition curator increasing concurrency=%s->%s", concurrency, next_concurrency)
            concurrency = next_concurrency
            stable_completions = 0
            transport_errors = 0

    return total, concurrency, frozen


async def _metadata_pause(seconds: float) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not STOP.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.25, remaining))


async def _metadata_loop(http_client: httpx2.AsyncClient) -> None:
    async def execute(work_id: UUID) -> RecipeMetadataReport | None:
        try:
            with session_context() as session:
                return await RecipeMetadataWorker(session, should_stop=STOP.is_set, http_client=http_client).run_work(
                    work_id
                )
        except Exception as exc:
            logger.error("Recipe metadata task failed error_type=%s", type(exc).__name__)
            return None

    cursor: UUID | None = None
    poll = get_app_settings().HEALTHLABS_NUTRITION_CURATOR_POLL_SECONDS
    while not STOP.is_set():
        try:
            with session_context() as session:
                ids = RecipeMetadataWorker(session, should_stop=STOP.is_set).work_ids(
                    limit=8, after=cursor, keyset=True
                )
            if not ids:
                cursor = None
                await _metadata_pause(poll)
                continue
            # Advance over INSPECTED rows even when credentials/claims fail.
            # An empty page ends the pass and backs off before revisiting it.
            cursor = ids[-1]
            reports = await asyncio.gather(*(execute(work_id) for work_id in ids))
            if any(report is not None and report.stopped for report in reports):
                return
            if any(
                report is not None
                and (report.provider_exhausted or report.local_limit_reached or report.failed or report.scope_changed)
                for report in reports
            ):
                await _metadata_pause(poll)
        except Exception as exc:
            logger.error("Recipe metadata selection failed error_type=%s", type(exc).__name__)
            await _metadata_pause(poll)


async def _curator_loop(http_client: httpx2.AsyncClient) -> None:
    settings = get_app_settings()
    concurrency = min(
        settings.HEALTHLABS_NUTRITION_CURATOR_CONCURRENCY, settings.HEALTHLABS_NUTRITION_CURATOR_MAX_CONCURRENCY
    )
    frozen = False
    cursor: UUID | None = None
    while not STOP.is_set():
        with session_context() as session:
            work_ids = NutritionCuratorWorker(session, should_stop=STOP.is_set).work_ids(limit=64, after=cursor)
        # Bound inspected IDs, including terminal/no-op prefixes. UUID keyset
        # progression reaches later targets even when no decision is claimed.
        cursor = work_ids[-1] if work_ids else None
        report, concurrency, frozen = await _drain(
            work_ids,
            initial_concurrency=concurrency,
            max_concurrency=settings.HEALTHLABS_NUTRITION_CURATOR_MAX_CONCURRENCY,
            frozen=frozen,
            http_client=http_client,
        )
        logger.info(
            "Nutrition curator claimed=%s approved=%s insufficient_evidence=%s failed=%s concurrency=%s",
            report.claimed,
            report.approved,
            report.insufficient_evidence,
            report.failed,
            concurrency,
        )
        if STOP.is_set():
            break
        if not work_ids or report.provider_exhausted:
            await asyncio.to_thread(STOP.wait, settings.HEALTHLABS_NUTRITION_CURATOR_POLL_SECONDS)
        else:
            await asyncio.sleep(0)


async def _main() -> int:
    settings = get_app_settings()
    if not settings.HEALTHLABS_NUTRITION_CURATOR_ENABLED:
        return 0
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    limits = httpx2.Limits(
        max_connections=settings.HEALTHLABS_NUTRITION_CURATOR_MAX_CONCURRENCY + 8,
        max_keepalive_connections=min(512, settings.HEALTHLABS_NUTRITION_CURATOR_MAX_CONCURRENCY + 8),
        keepalive_expiry=30,
    )
    async with httpx2.AsyncClient(limits=limits, follow_redirects=True) as http_client:
        # Independent bounded consumers share process shutdown and the global
        # provider ledger. Long curator I/O never blocks metadata dispatch.
        await asyncio.gather(_metadata_loop(http_client), _curator_loop(http_client))
    return 0


def main() -> int:
    return asyncio.run(_main())
