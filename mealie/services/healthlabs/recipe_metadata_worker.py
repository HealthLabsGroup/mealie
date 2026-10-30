"""One shared atomic claim/finalize path for session operators and normal workers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from httpx2 import AsyncClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from mealie.core.config import get_app_settings
from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate as Job
from mealie.repos.repository_factory import AllRepositories
from mealie.services.healthlabs.nutrition_provider_budget import NutritionProviderBudget
from mealie.services.healthlabs.nutrition_provider_resolution import (
    CuratorProviderResolver,
    ResolvedCuratorProvider,
    resolve_default_provider,
)
from mealie.services.healthlabs.recipe_metadata_estimator import (
    PROMPT,
    InvalidMetadataPlan,
    MetadataLimitReason,
    MetadataRequestLimited,
    MetadataRequestLimits,
    MetadataTransportResponse,
    RecipeMetadataEstimator,
)
from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler
from mealie.services.healthlabs.recipe_metadata_service import POLICY_VERSION, RecipeMetadataService
from mealie.services.openai import OpenAIService
from mealie.services.openai.openai import OpenAIRequestRejected


@dataclass(frozen=True)
class RecipeMetadataReport:
    claimed: int = 0
    ready: int = 0
    failed: int = 0
    rejected: int = 0
    superseded: int = 0
    provider_exhausted: bool = False
    requests: int = 0
    local_limit_reached: bool = False
    stopped: bool = False
    limit_reason: MetadataLimitReason | None = None


class RecipeMetadataWorker:
    """A worker owns one Session; never share it between concurrent tasks.

    run_once and run_work are async. work_ids is a bounded, due-only selector;
    callers commit after discovery and create independent workers per task.
    """

    def __init__(
        self,
        session: Session,
        *,
        country: str = "GB",
        now: datetime | None = None,
        provider_resolver: CuratorProviderResolver | None = None,
        group_ids: set[UUID] | frozenset[UUID] | None = None,
        recipe_ids: set[UUID] | frozenset[UUID] | None = None,
        limits: MetadataRequestLimits | None = None,
        http_client: AsyncClient | None = None,
        should_stop: Callable[[], bool] = lambda: False,
        max_attempts: int = 3,
        corrections: int = 1,
    ) -> None:
        if not country.strip() or not 1 <= max_attempts <= 10:
            raise ValueError("invalid country/attempt limit")
        if provider_resolver is not None and group_ids is None:
            raise ValueError("explicit provider resolver requires a group allowlist")
        self.session, self.country, self._fixed_now = session, country.upper(), now
        self._uses_default_provider = provider_resolver is None
        self.provider_resolver = provider_resolver or resolve_default_provider
        self.group_ids = frozenset(group_ids) if group_ids is not None else None
        self.recipe_ids = frozenset(recipe_ids) if recipe_ids is not None else None
        self.limits = limits or MetadataRequestLimits()
        self.http_client, self.should_stop, self.max_attempts = http_client, should_stop, max_attempts
        self.estimator = RecipeMetadataEstimator(limits=self.limits, corrections=corrections)
        self.budget = NutritionProviderBudget.from_settings(session, get_app_settings(), now=self._now)
        self.requests = 0
        self.usage: dict[str, int] = {}

    def _now(self) -> datetime:
        return self._fixed_now or get_utc_now()

    def _scope(self) -> list[sa.ColumnElement[bool]]:
        filters = [Job.policy_version == POLICY_VERSION]
        if self.group_ids is not None:
            filters.append(Job.group_id.in_(self.group_ids))
        if self.recipe_ids is not None:
            filters.append(Job.recipe_id.in_(self.recipe_ids))
        return filters

    def _due(self) -> sa.ColumnElement[bool]:
        return sa.or_(
            sa.and_(
                Job.state.in_(["pending", "failed"]),
                Job.next_attempt_at <= self._now(),
                Job.attempt_count < self.max_attempts,
            ),
            sa.and_(
                Job.state == "running", sa.or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= self._now())
            ),
        )

    def work_ids(self, *, limit: int = 20) -> list[UUID]:
        if not 1 <= limit <= 1000:
            raise ValueError("selection limit must be 1..1000")
        if self.should_stop():
            return []
        query = sa.select(Job.id)
        if self._uses_default_provider:
            query = query.join(AIProviderSettings, AIProviderSettings.group_id == Job.group_id).join(
                AIProvider,
                sa.and_(
                    AIProvider.id == AIProviderSettings.default_provider_id,
                    AIProvider.settings_id == AIProviderSettings.id,
                ),
            )
        return list(
            self.session.scalars(
                query.where(*self._scope(), self._due()).order_by(Job.next_attempt_at, Job.id).limit(limit)
            )
        )

    async def run_once(self) -> RecipeMetadataReport:
        if self.should_stop():
            return RecipeMetadataReport(stopped=True, limit_reason="operator_stopped")
        ids = self.work_ids()
        self.session.commit()
        for work_id in ids:
            report = await self.run_work(work_id)
            if report.claimed or report.stopped or report.local_limit_reached:
                return report
        return RecipeMetadataReport()

    async def run_once_async(self) -> RecipeMetadataReport:
        return await self.run_once()

    def _lease(self, work_id: UUID, token: UUID) -> sa.ColumnElement[bool]:
        return sa.and_(
            Job.id == work_id, Job.state == "running", Job.lease_token == token, Job.lease_expires_at > self._now()
        )

    def _finish(self, work_id: UUID, token: UUID, **values: Any) -> bool:
        if self.usage:
            values["usage"] = dict(self.usage)
        changed = (
            self.session.execute(
                sa.update(Job)
                .where(self._lease(work_id, token))
                .values(
                    **values,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=self._now(),
                )
            ).rowcount
            == 1
        )
        self.session.commit()
        return changed

    async def _request(
        self,
        service: OpenAIService,
        provider: ResolvedCuratorProvider,
        message: str,
        schema: type[MetadataTransportResponse],
        limits: MetadataRequestLimits,
    ) -> MetadataTransportResponse | None:
        reservation = None
        usage = None

        def reserve(input_tokens: int, output_tokens: int) -> bool:
            nonlocal reservation
            cost = self.budget.cost_microusd(input_tokens, output_tokens)
            if self.should_stop():
                raise MetadataRequestLimited("operator_stopped")
            if input_tokens > limits.max_input_tokens:
                raise MetadataRequestLimited("input_limit_exceeded")
            if self.usage.get("requests", 0) >= limits.max_requests_per_job:
                raise MetadataRequestLimited("job_request_limit_exceeded")
            if limits.requests >= limits.max_requests:
                raise MetadataRequestLimited("operator_request_limit")
            if limits.reserved_microusd + cost > limits.max_cost_microusd:
                raise MetadataRequestLimited("operator_cost_limit")
            if not self.budget.enabled:
                return False
            reservation = self.budget.reserve(input_tokens, output_tokens)
            if reservation is None:
                return False
            limits.requests += 1
            limits.reserved_microusd += cost
            self.requests += 1
            self.usage["requests"] = self.usage.get("requests", 0) + 1
            return True

        def capture(value: dict[str, int]) -> None:
            nonlocal usage
            usage = dict(value)

        # Resolve all transport fields, including lazy stored credentials, BEFORE commit.
        transport = provider.transport()
        self.session.commit()
        try:
            response = await service.get_response(
                PROMPT,
                message,
                response_schema=schema,
                provider=transport,
                max_completion_tokens=limits.max_output_tokens,
                request_guard=reserve,
                usage_callback=capture,
                disable_reasoning=True,
            )
            if reservation is None or usage is None:
                raise RuntimeError("missing reserved token accounting")
            return response
        except Exception as exc:
            # OpenAIService wraps parse exceptions; preserve only a sanitized category.
            cause: BaseException = exc
            while cause.__cause__ is not None:
                cause = cause.__cause__
            if isinstance(cause, ValidationError):
                raise InvalidMetadataPlan("invalid_schema_or_numeric_value") from None
            raise
        finally:
            if reservation is not None and usage is not None:
                self.budget.record(reservation, usage)
                for key, value in usage.items():
                    self.usage[key] = self.usage.get(key, 0) + value
            elif reservation is not None:
                # Unknown usage remains conservatively reserved by the shared budget.
                pass

    async def run_work(self, work_id: UUID) -> RecipeMetadataReport:
        if self.should_stop():
            return RecipeMetadataReport(stopped=True, limit_reason="operator_stopped")
        self.session.expire_all()
        row = self.session.scalar(sa.select(Job).where(Job.id == work_id, *self._scope(), self._due()))
        if row is None:
            self.session.rollback()
            return RecipeMetadataReport()
        group_id, recipe_id, fingerprint = row.group_id, row.recipe_id, row.input_fingerprint
        attempt = row.attempt_count
        # Valid native drafts can have no ingredients. Close legacy jobs before
        # resolving providers or credentials, even when their fingerprint is old.
        repos = AllRepositories(self.session, group_id=group_id, household_id=None)
        recipe = repos.recipes.get_one(recipe_id, "id")
        if recipe is None:
            self.session.rollback()
            return RecipeMetadataReport()
        if not recipe.recipe_ingredient:
            changed = (
                self.session.execute(
                    sa.update(Job)
                    .where(Job.id == work_id, *self._scope(), self._due())
                    .values(
                        state="superseded",
                        last_error_code="empty_ingredients",
                        lease_token=None,
                        lease_expires_at=None,
                        payload=None,
                        updated_at=self._now(),
                    )
                ).rowcount
                == 1
            )
            self.session.commit()
            return RecipeMetadataReport(claimed=int(changed), superseded=int(changed))
        provider = self.provider_resolver(self.session, group_id)
        if provider is None or provider.group_id != group_id:
            self.session.rollback()
            return RecipeMetadataReport()
        # Freeze memory transport before the commit expires any ORM-backed resolver fields.
        transport = provider.transport()
        provider = ResolvedCuratorProvider(
            group_id=provider.group_id,
            identity_id=provider.identity_id,
            audit_provider_id=provider.audit_provider_id,
            name=provider.name,
            base_url=provider.base_url,
            model=provider.model,
            timeout=provider.timeout,
            request_headers=provider.request_headers,
            request_params=provider.request_params,
            _credential=lambda: transport.api_key,
        )
        token = uuid4()
        claimed = self.session.execute(
            sa.update(Job)
            .where(Job.id == work_id, *self._scope(), self._due())
            .values(
                state="running",
                provider_name=provider.name,
                provider_model=provider.model,
                lease_token=token,
                lease_expires_at=self._now() + timedelta(minutes=15),
                attempt_count=Job.attempt_count + 1,
                updated_at=self._now(),
                last_error_code=None,
            )
        ).rowcount
        self.session.commit()
        if claimed != 1:
            return RecipeMetadataReport()
        if attempt >= self.max_attempts:
            self._finish(work_id, token, state="failed", last_error_code="attempts_exhausted")
            return RecipeMetadataReport(claimed=1, failed=1)
        requests_before = self.requests
        self.usage = {}
        try:
            repos = AllRepositories(self.session, group_id=group_id, household_id=None)
            recipe = repos.recipes.get_one(recipe_id, "id")
            if recipe is None:
                self.session.rollback()
                return RecipeMetadataReport(claimed=1)
            service = RecipeMetadataService(self.session, country=self.country)
            inputs = service.inputs(recipe)
            if inputs.input_fingerprint != fingerprint or not inputs.ingredients or not inputs.missing_fields():
                self._finish(work_id, token, state="superseded")
                RecipeMetadataScheduler(self.session, country=self.country, now=self._now()).schedule(recipe)
                self.session.commit()
                return RecipeMetadataReport(claimed=1, superseded=1)
            openai = OpenAIService(repos, http_client=self.http_client, sdk_max_retries=0)
            self.session.commit()
            # One total deadline covers every chunk/correction, leaving 60s to finalize.
            async with asyncio.timeout(14 * 60):
                payload = await self.estimator.estimate(inputs, provider, openai, request=self._request)
            self.session.expire_all()
            fresh_recipe = repos.recipes.get_one(recipe_id, "id")
            if fresh_recipe is None:
                self.session.rollback()
                return RecipeMetadataReport(claimed=1)
            fresh = service.inputs(fresh_recipe)
            if fresh.input_fingerprint != fingerprint:
                self._finish(work_id, token, state="superseded")
                RecipeMetadataScheduler(self.session, country=self.country, now=self._now()).schedule(fresh_recipe)
                self.session.commit()
                return RecipeMetadataReport(claimed=1, superseded=1, requests=self.requests - requests_before)
            applied = self._finish(
                work_id,
                token,
                state="ready",
                payload=payload.model_dump(mode="json"),
                provider_name=provider.name,
                provider_model=provider.model,
            )
            return RecipeMetadataReport(claimed=1, ready=int(applied), requests=self.requests - requests_before)
        except MetadataRequestLimited as exc:
            self.session.rollback()
            requested = self.requests - requests_before
            terminal = exc.reason in {"input_limit_exceeded", "job_request_limit_exceeded"}
            self._finish(
                work_id,
                token,
                state="rejected" if terminal else "pending",
                next_attempt_at=self._now(),
                attempt_count=attempt + int(terminal and bool(requested)),
                last_error_code=exc.reason,
            )
            return RecipeMetadataReport(
                claimed=1,
                rejected=int(terminal),
                requests=requested,
                local_limit_reached=exc.reason in {"operator_request_limit", "operator_cost_limit"},
                stopped=exc.reason == "operator_stopped",
                limit_reason=exc.reason,
            )
        except OpenAIRequestRejected:
            self.session.rollback()
            requested = self.requests - requests_before
            tomorrow = datetime.combine(self._now().date() + timedelta(days=1), time.min, tzinfo=UTC)
            self._finish(
                work_id,
                token,
                state="pending",
                next_attempt_at=tomorrow,
                attempt_count=attempt + int(bool(requested)),
                last_error_code="budget_deferred",
            )
            return RecipeMetadataReport(claimed=1, provider_exhausted=True, requests=requested)
        except InvalidMetadataPlan:
            self.session.rollback()
            self._finish(work_id, token, state="rejected", last_error_code="invalid_plan")
            return RecipeMetadataReport(claimed=1, rejected=1, requests=self.requests - requests_before)
        except Exception:
            self.session.rollback()
            self._finish(
                work_id,
                token,
                state="failed",
                last_error_code="provider_failure",
                next_attempt_at=self._now() + timedelta(minutes=min(60, 2 ** (attempt + 1))),
            )
            return RecipeMetadataReport(claimed=1, failed=1, requests=self.requests - requests_before)
