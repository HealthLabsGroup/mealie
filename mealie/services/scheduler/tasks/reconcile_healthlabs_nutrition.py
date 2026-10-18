import asyncio
from threading import Event, Lock
from time import monotonic

from mealie.core import root_logger
from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.services.healthlabs.nutrition_worker import NutritionReconciliationWorker

logger = root_logger.get_logger()
_run_lock = Lock()
_shutdown_requested = Event()
MAX_TICK_SECONDS = 4 * 60


def request_nutrition_reconciliation_shutdown() -> None:
    _shutdown_requested.set()


def resume_nutrition_reconciliation() -> None:
    _shutdown_requested.clear()


def reconcile_healthlabs_nutrition() -> None:
    if _shutdown_requested.is_set():
        return
    if not _run_lock.acquire(blocking=False):
        return

    try:
        settings = get_app_settings()
        deadline = monotonic() + MAX_TICK_SECONDS
        processed = completed = review_required = failed = 0
        with session_context() as session:
            while True:
                report = asyncio.run(
                    NutritionReconciliationWorker(
                        session,
                        batch_size=20,
                        provider_daily_request_limit=settings.HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT,
                        provider_daily_budget_usd=settings.HEALTHLABS_NUTRITION_PROVIDER_DAILY_BUDGET_USD,
                        provider_input_usd_per_million_tokens=(
                            settings.HEALTHLABS_NUTRITION_PROVIDER_INPUT_USD_PER_MILLION_TOKENS
                        ),
                        provider_output_usd_per_million_tokens=(
                            settings.HEALTHLABS_NUTRITION_PROVIDER_OUTPUT_USD_PER_MILLION_TOKENS
                        ),
                        provider_max_output_tokens=settings.HEALTHLABS_NUTRITION_PROVIDER_MAX_OUTPUT_TOKENS,
                        should_stop=_shutdown_requested.is_set,
                    ).run()
                )
                processed += report.processed
                completed += report.completed
                review_required += report.review_required
                failed += report.failed
                if (
                    (report.discovered == 0 and report.processed == 0)
                    or report.provider_exhausted
                    or _shutdown_requested.is_set()
                    or monotonic() >= deadline
                ):
                    break
        logger.info(
            "Nutrition reconciliation processed=%s completed=%s review_required=%s failed=%s",
            processed,
            completed,
            review_required,
            failed,
        )
    except Exception as exc:
        logger.error("Nutrition reconciliation failed: %s", type(exc).__name__)
    finally:
        _run_lock.release()
