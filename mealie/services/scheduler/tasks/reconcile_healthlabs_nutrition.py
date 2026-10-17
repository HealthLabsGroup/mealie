import asyncio
from threading import Lock

from mealie.core import root_logger
from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.services.healthlabs.nutrition_worker import NutritionReconciliationWorker

logger = root_logger.get_logger()
_run_lock = Lock()


def reconcile_healthlabs_nutrition() -> None:
    if not _run_lock.acquire(blocking=False):
        return

    try:
        settings = get_app_settings()
        with session_context() as session:
            report = asyncio.run(
                NutritionReconciliationWorker(
                    session,
                    batch_size=20,
                    provider_daily_request_limit=settings.HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT,
                ).run()
            )
        logger.info(
            "Nutrition reconciliation processed=%s completed=%s review_required=%s failed=%s",
            report.processed,
            report.completed,
            report.review_required,
            report.failed,
        )
    except Exception as exc:
        logger.error("Nutrition reconciliation failed: %s", type(exc).__name__)
    finally:
        _run_lock.release()
