from __future__ import annotations

import signal
from threading import Event

from mealie.core import root_logger
from mealie.core.config import get_app_settings
from mealie.db.db_setup import session_context
from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorWorker

logger = root_logger.get_logger(__name__)
STOP = Event()


def _request_stop(_signum, _frame) -> None:
    STOP.set()


def main() -> int:
    settings = get_app_settings()
    if not settings.HEALTHLABS_NUTRITION_CURATOR_ENABLED:
        return 0
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    while not STOP.is_set():
        with session_context() as session:
            report = NutritionCuratorWorker(session, should_stop=STOP.is_set).run_once()
        logger.info(
            "Nutrition curator claimed=%s approved=%s insufficient_evidence=%s failed=%s",
            report.claimed,
            report.approved,
            report.insufficient_evidence,
            report.failed,
        )
        if STOP.is_set():
            break
        if report.claimed == 0:
            STOP.wait(settings.HEALTHLABS_NUTRITION_CURATOR_POLL_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
