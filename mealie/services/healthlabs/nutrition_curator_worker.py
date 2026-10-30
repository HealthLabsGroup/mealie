from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import sqlalchemy as sa
from httpx2 import AsyncClient
from sqlalchemy.orm import Session

from mealie.core import exceptions, root_logger
from mealie.core.config import get_app_settings
from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import HealthLabsNutritionCuratorDecision, HealthLabsNutritionWork
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.nutrition_curator import NutritionCuratorProviderDecision
from mealie.services.healthlabs.nutrition_curator_prompt import NutritionCuratorPrompt
from mealie.services.healthlabs.nutrition_curator_service import (
    DECISION_VERSION,
    FinalizedCuratorDecision,
    NutritionCuratorService,
    StartedCuratorDecision,
)
from mealie.services.healthlabs.nutrition_provider_budget import NutritionProviderBudget, ProviderReservation
from mealie.services.healthlabs.nutrition_provider_resolution import CuratorProviderResolver
from mealie.services.healthlabs.recipe_metadata_estimator import MetadataRequestLimited, MetadataRequestLimits
from mealie.services.healthlabs.recipe_metadata_source_refresh import (
    RecipeAuthorizationChanged,
    RecipeAuthorizationGuard,
    source_target_authorized,
)
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
    provider_error: bool = False
    throttled: bool = False
    transport_error: bool = False
    local_limit_reached: bool = False
    stopped: bool = False
    limit_reason: str | None = None
    scope_changed: bool = False


class NutritionCuratorWorker:
    def __init__(
        self,
        session: Session,
        *,
        country: str = "GB",
        lease_minutes: int = 15,
        now: datetime | None = None,
        should_stop: Callable[[], bool] = lambda: False,
        http_client: AsyncClient | None = None,
        provider_resolver: CuratorProviderResolver | None = None,
        group_ids: set[UUID] | frozenset[UUID] | None = None,
        work_ids: set[UUID] | frozenset[UUID] | None = None,
        recipe_ids: set[UUID] | frozenset[UUID] | None = None,
        recipe_guard: RecipeAuthorizationGuard | None = None,
        request_limits: MetadataRequestLimits | None = None,
        sdk_max_retries: int | None = None,
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
        self.http_client = http_client
        self.provider_resolver = provider_resolver
        self.group_ids = frozenset(UUID(str(group_id)) for group_id in group_ids) if group_ids is not None else None
        self.allowed_work_ids = frozenset(work_ids) if work_ids is not None else None
        self.recipe_ids = recipe_ids
        self.recipe_guard = recipe_guard
        self.request_limits = request_limits
        self.sdk_max_retries = sdk_max_retries
        self.settings = get_app_settings()
        self.budget = NutritionProviderBudget.from_settings(session, self.settings, now=lambda: self._current_time())

    def work_ids(self, *, limit: int | None = None, after: UUID | None = None) -> list[UUID]:
        if limit is not None and not 1 <= limit <= 1000:
            raise ValueError("curator selection limit must be 1..1000")
        if self.should_stop() or not self.settings.HEALTHLABS_NUTRITION_CURATOR_ENABLED or not self.budget.enabled:
            return []
        now = self._current_time()
        due_existing_decision = sa.exists().where(
            HealthLabsNutritionCuratorDecision.work_id == HealthLabsNutritionWork.id,
            sa.or_(
                sa.and_(
                    HealthLabsNutritionCuratorDecision.state == "pending",
                    HealthLabsNutritionCuratorDecision.next_attempt_at <= now,
                ),
                sa.and_(
                    HealthLabsNutritionCuratorDecision.state == "running",
                    sa.or_(
                        HealthLabsNutritionCuratorDecision.lease_expires_at.is_(None),
                        HealthLabsNutritionCuratorDecision.lease_expires_at <= now,
                    ),
                ),
            ),
        )
        old_failed_contract = sa.exists().where(
            HealthLabsNutritionCuratorDecision.work_id == HealthLabsNutritionWork.id,
            HealthLabsNutritionCuratorDecision.state == "failed",
            HealthLabsNutritionCuratorDecision.reason_code == "provider_failure",
            HealthLabsNutritionCuratorDecision.reuse_sha256.is_not(None),
            HealthLabsNutritionCuratorDecision.prompt_version != DECISION_VERSION,
        )
        current_contract_attempt = sa.exists().where(
            HealthLabsNutritionCuratorDecision.work_id == HealthLabsNutritionWork.id,
            HealthLabsNutritionCuratorDecision.prompt_version == DECISION_VERSION,
        )
        query = sa.select(HealthLabsNutritionWork.id)
        if self.provider_resolver is None or self.group_ids is None:
            query = query.join(
                AIProviderSettings, AIProviderSettings.group_id == HealthLabsNutritionWork.group_id
            ).join(
                AIProvider,
                sa.and_(
                    AIProvider.id == AIProviderSettings.default_provider_id,
                    AIProvider.settings_id == AIProviderSettings.id,
                ),
            )
        if self.group_ids is not None:
            query = query.where(HealthLabsNutritionWork.group_id.in_(self.group_ids))
        if self.allowed_work_ids is not None:
            query = query.where(HealthLabsNutritionWork.id.in_(self.allowed_work_ids))
        if after is not None:
            query = query.where(HealthLabsNutritionWork.id > after)
        if limit is not None:
            return list(
                self.session.scalars(
                    query.where(
                        HealthLabsNutritionWork.state == "review_required",
                        HealthLabsNutritionWork.next_attempt_at <= now,
                    )
                    .order_by(HealthLabsNutritionWork.id)
                    .limit(limit)
                )
            )
        return list(
            self.session.scalars(
                query.where(
                    HealthLabsNutritionWork.state == "review_required",
                    HealthLabsNutritionWork.next_attempt_at <= now,
                ).order_by(
                    sa.case(
                        (due_existing_decision, 0),
                        (sa.and_(old_failed_contract, ~current_contract_attempt), 1),
                        else_=2,
                    ),
                    HealthLabsNutritionWork.next_attempt_at,
                    HealthLabsNutritionWork.kind,
                    HealthLabsNutritionWork.target_id,
                    HealthLabsNutritionWork.id,
                )
            )
        )

    def run_once(self) -> NutritionCuratorReport:
        return asyncio.run(self.run_once_async())

    async def run_once_async(self) -> NutritionCuratorReport:
        empty = NutritionCuratorReport(0, 0, 0, 0, False)
        for work_id in self.work_ids():
            report = await self.run_work(work_id)
            if report.claimed:
                return report
        self.session.commit()
        return empty

    async def run_work(self, work_id: UUID) -> NutritionCuratorReport:
        empty = NutritionCuratorReport(0, 0, 0, 0, False)
        if self.should_stop() or not self.settings.HEALTHLABS_NUTRITION_CURATOR_ENABLED or not self.budget.enabled:
            return empty
        if self.allowed_work_ids is not None and work_id not in self.allowed_work_ids:
            return empty
        if self.group_ids is not None:
            group_id = self.session.scalar(
                sa.select(HealthLabsNutritionWork.group_id).where(HealthLabsNutritionWork.id == work_id)
            )
            if group_id not in self.group_ids:
                return empty
        now = self._current_time()
        started = NutritionCuratorService(
            self.session,
            country=self.country,
            now=now,
            provider_resolver=self.provider_resolver,
            refresh_recipe_ids=self.recipe_ids,
            recipe_guard=self.recipe_guard,
        ).start(work_id)
        if isinstance(started, FinalizedCuratorDecision):
            self.session.commit()
            return NutritionCuratorReport(
                claimed=1,
                approved=int(started.state == "approved"),
                insufficient_evidence=int(started.state == "insufficient_evidence"),
                failed=0,
                provider_exhausted=False,
            )
        if started is None:
            self.session.rollback()
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
            if not source_target_authorized(
                self.session,
                group_id=decision.group_id,
                kind=decision.kind,
                target_id=decision.target_id,
                recipe_ids=self.recipe_ids,
                recipe_guard=self.recipe_guard,
            ):
                raise RecipeAuthorizationChanged
            if self.should_stop():
                raise MetadataRequestLimited("operator_stopped")
            limits = self.request_limits
            cost = self.budget.cost_microusd(input_tokens, output_tokens)
            if limits is not None:
                if limits.requests >= limits.max_requests:
                    raise MetadataRequestLimited("operator_request_limit")
                if limits.reserved_microusd + cost > limits.max_cost_microusd:
                    raise MetadataRequestLimited("operator_cost_limit")
            reservation = self.budget.reserve(input_tokens, output_tokens)
            if reservation is not None and limits is not None:
                limits.requests += 1
                limits.reserved_microusd += cost
            return reservation is not None

        def record_usage(value: dict[str, int]) -> None:
            nonlocal usage
            usage = dict(value)

        try:
            repos = AllRepositories(self.session, group_id=decision.group_id, household_id=None)
            provider_service = OpenAIService(repos, http_client=self.http_client, sdk_max_retries=self.sdk_max_retries)
            # Release the synchronous DB connection before this task awaits provider I/O.
            self.session.commit()
            response = await self._request(
                provider_service,
                started,
                reserve=reserve,
                record_usage=record_usage,
            )
            if response is None or reservation is None or usage is None:
                raise RuntimeError("provider response did not include reserved token usage")
            self.budget.record(reservation, usage)
            scope_changed = not source_target_authorized(
                self.session,
                group_id=decision.group_id,
                kind=decision.kind,
                target_id=decision.target_id,
                recipe_ids=self.recipe_ids,
                recipe_guard=self.recipe_guard,
            )
            state = NutritionCuratorService(
                self.session,
                country=self.country,
                now=now,
                provider_resolver=self.provider_resolver,
                refresh_recipe_ids=self.recipe_ids,
                recipe_guard=self.recipe_guard,
            ).apply(
                started.decision_id,
                response,
                usage,
            )
            self.session.commit()
            return NutritionCuratorReport(
                claimed=1,
                approved=int(state == "approved"),
                scope_changed=scope_changed,
                insufficient_evidence=int(state == "insufficient_evidence"),
                failed=0,
                provider_exhausted=False,
            )
        except RecipeAuthorizationChanged:
            self.session.rollback()
            NutritionCuratorService(
                self.session, country=self.country, now=self._current_time(), provider_resolver=self.provider_resolver
            ).defer_until(started.decision_id, self._current_time(), "manifest_scope_changed")
            self.session.commit()
            return NutritionCuratorReport(1, 0, 0, 0, False, scope_changed=True)
        except MetadataRequestLimited as exc:
            self.session.rollback()
            NutritionCuratorService(
                self.session, country=self.country, now=self._current_time(), provider_resolver=self.provider_resolver
            ).defer_until(started.decision_id, self._current_time(), exc.reason)
            self.session.commit()
            return NutritionCuratorReport(
                1,
                0,
                0,
                0,
                False,
                local_limit_reached=exc.reason != "operator_stopped",
                stopped=exc.reason == "operator_stopped",
                limit_reason=exc.reason,
            )
        except OpenAIRequestRejected as exc:
            self.session.rollback()
            finished_at = self._current_time()
            if reservation is not None:
                return self._fail(started.decision_id, exc, finished_at)
            tomorrow = datetime.combine(finished_at.date() + timedelta(days=1), time.min, tzinfo=UTC)
            NutritionCuratorService(
                self.session, country=self.country, now=finished_at, provider_resolver=self.provider_resolver
            ).defer_until(
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
        cause = exc.__cause__ or exc
        logger.warning("Nutrition curator provider failure error_type=%s", type(cause).__name__)
        state = NutritionCuratorService(
            self.session, country=self.country, now=now, provider_resolver=self.provider_resolver
        ).fail(decision_id, exc)
        self.session.commit()
        return NutritionCuratorReport(
            1,
            0,
            0,
            int(state == "failed"),
            False,
            provider_error=True,
            throttled=isinstance(exc, exceptions.RateLimitError),
            transport_error=type(cause).__name__ in {"APIConnectionError", "APITimeoutError", "OSError"},
        )

    def _current_time(self) -> datetime:
        return self._fixed_now or get_utc_now()
