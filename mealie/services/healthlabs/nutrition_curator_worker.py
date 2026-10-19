from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.core import root_logger
from mealie.core.config import get_app_settings
from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import HealthLabsNutritionCuratorDecision, HealthLabsNutritionWork
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.nutrition_curator import NutritionCuratorProviderDecision
from mealie.services.healthlabs.nutrition_curator_prompt import NutritionCuratorPrompt
from mealie.services.healthlabs.nutrition_curator_service import (
    FinalizedCuratorDecision,
    NutritionCuratorService,
    StartedCuratorDecision,
)
from mealie.services.healthlabs.nutrition_provider_budget import NutritionProviderBudget, ProviderReservation
from mealie.services.openai import OpenAIService
from mealie.services.openai.openai import OpenAIRequestRejected

PROVIDER_CALL_TIMEOUT_SECONDS = 14 * 60
FINALIZATION_MARGIN_SECONDS = 60
logger = root_logger.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class NutritionCuratorReport:
    claimed: int
    approved: int
    insufficient_evidence: int
    failed: int
    provider_exhausted: bool


class NutritionCuratorWorker:
    def __init__(
        self,
        session: Session,
        *,
        country: str = "GB",
        lease_minutes: int = 15,
        now: datetime | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        if not (country := country.strip().upper()):
            raise ValueError("country is required")
        lease_seconds = lease_minutes * 60
        if lease_seconds <= FINALIZATION_MARGIN_SECONDS:
            raise ValueError("lease_minutes must leave time for finalization")
        self.session = session
        self.country = country
        self.lease_duration = timedelta(minutes=lease_minutes)
        self.provider_timeout_seconds = min(
            PROVIDER_CALL_TIMEOUT_SECONDS,
            lease_seconds - FINALIZATION_MARGIN_SECONDS,
        )
        self._fixed_now = now
        self.should_stop = should_stop
        self.settings = get_app_settings()
        self.budget = NutritionProviderBudget.from_settings(session, self.settings, now=lambda: self._current_time())

    def run_once(self) -> NutritionCuratorReport:
        empty = NutritionCuratorReport(0, 0, 0, 0, False)
        if self.should_stop() or not self.settings.HEALTHLABS_NUTRITION_CURATOR_ENABLED or not self.budget.enabled:
            return empty
        work_ids = self.session.scalars(
            sa.select(HealthLabsNutritionWork.id)
            .join(AIProviderSettings, AIProviderSettings.group_id == HealthLabsNutritionWork.group_id)
            .join(
                AIProvider,
                sa.and_(
                    AIProvider.id == AIProviderSettings.default_provider_id,
                    AIProvider.settings_id == AIProviderSettings.id,
                ),
            )
            .where(
                HealthLabsNutritionWork.state == "review_required",
                HealthLabsNutritionWork.next_attempt_at <= self._current_time(),
            )
            .order_by(
                HealthLabsNutritionWork.next_attempt_at,
                HealthLabsNutritionWork.kind,
                HealthLabsNutritionWork.target_id,
                HealthLabsNutritionWork.id,
            )
        ).all()
        now = self._current_time()
        started = None
        for work_id in work_ids:
            if self.should_stop():
                break
            started = NutritionCuratorService(self.session, country=self.country, now=now).start(work_id)
            if isinstance(started, FinalizedCuratorDecision):
                self.session.commit()
                return NutritionCuratorReport(
                    claimed=1,
                    approved=int(started.state == "approved"),
                    insufficient_evidence=int(started.state == "insufficient_evidence"),
                    failed=0,
                    provider_exhausted=False,
                )
            if started is not None:
                break
            self.session.rollback()
        if started is None:
            self.session.commit()
            return empty
        assert isinstance(started, StartedCuratorDecision)
        decision = self.session.get(HealthLabsNutritionCuratorDecision, started.decision_id)
        if decision is None:
            self.session.rollback()
            return empty
        decision.lease_expires_at = now + self.lease_duration
        self.session.commit()

        reservation: ProviderReservation | None = None
        usage: dict[str, int] | None = None

        def reserve(input_tokens: int, output_tokens: int) -> bool:
            nonlocal reservation
            reservation = self.budget.reserve(input_tokens, output_tokens)
            return reservation is not None

        def record_usage(value: dict[str, int]) -> None:
            nonlocal usage
            usage = dict(value)

        try:
            repos = AllRepositories(self.session, group_id=decision.group_id, household_id=None)
            response = asyncio.run(
                self._request(
                    OpenAIService(repos),
                    started,
                    reserve=reserve,
                    record_usage=record_usage,
                )
            )
            if response is None or reservation is None or usage is None:
                raise RuntimeError("provider response did not include reserved token usage")
            self.budget.record(reservation, usage)
            state = NutritionCuratorService(self.session, country=self.country, now=now).apply(
                started.decision_id,
                response,
                usage,
            )
            self.session.commit()
            return NutritionCuratorReport(
                claimed=1,
                approved=int(state == "approved"),
                insufficient_evidence=int(state == "insufficient_evidence"),
                failed=0,
                provider_exhausted=False,
            )
        except OpenAIRequestRejected as exc:
            self.session.rollback()
            finished_at = self._current_time()
            if reservation is not None:
                return self._fail(started.decision_id, exc, finished_at)
            tomorrow = datetime.combine(finished_at.date() + timedelta(days=1), time.min, tzinfo=UTC)
            NutritionCuratorService(self.session, country=self.country, now=finished_at).defer_until(
                started.decision_id,
                tomorrow,
                "provider_budget_exhausted",
            )
            self.session.commit()
            return NutritionCuratorReport(1, 0, 0, 0, True)
        except Exception as exc:
            self.session.rollback()
            return self._fail(started.decision_id, exc, self._current_time())

    async def _request(
        self,
        service: OpenAIService,
        started: StartedCuratorDecision,
        *,
        reserve: Callable[[int, int], bool],
        record_usage: Callable[[dict[str, int]], None],
    ) -> NutritionCuratorProviderDecision | None:
        async with asyncio.timeout(self.provider_timeout_seconds):
            return await service.get_response(
                NutritionCuratorPrompt.system(),
                started.prompt_payload,
                response_schema=started.response_schema,
                provider=started.provider,
                max_completion_tokens=self.settings.HEALTHLABS_NUTRITION_PROVIDER_MAX_OUTPUT_TOKENS,
                request_guard=reserve,
                usage_callback=record_usage,
                disable_reasoning=True,
            )

    def _fail(self, decision_id: UUID, exc: Exception, now: datetime) -> NutritionCuratorReport:
        logger.warning("Nutrition curator provider failure error_type=%s", type(exc.__cause__ or exc).__name__)
        state = NutritionCuratorService(self.session, country=self.country, now=now).fail(decision_id, exc)
        self.session.commit()
        return NutritionCuratorReport(1, 0, 0, int(state == "failed"), False)

    def _current_time(self) -> datetime:
        return self._fixed_now or get_utc_now()
