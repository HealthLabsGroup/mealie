from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import HealthLabsNutritionProviderUsage, HealthLabsNutritionWork
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.healthlabs.nutrition import (
    HealthLabsNutritionWorkerStatus,
    HealthLabsNutritionWorkKind,
    HealthLabsNutritionWorkOut,
    HealthLabsNutritionWorkState,
)
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import RecipeAccessPolicy

WORK_STATES = tuple(state.value for state in HealthLabsNutritionWorkState)


class NutritionWorkService:
    def __init__(
        self,
        session: Session,
        user: PrivateUser,
        *,
        provider_daily_request_limit: int,
    ) -> None:
        self.session = session
        self.user = user
        self.provider_daily_request_limit = provider_daily_request_limit
        self.policy = RecipeAccessPolicy(session, user)

    def status(self) -> HealthLabsNutritionWorkerStatus:
        self._require_write()
        counts = dict.fromkeys(WORK_STATES, 0)
        counts.update(
            dict(
                self.session.execute(
                    sa.select(HealthLabsNutritionWork.state, sa.func.count())
                    .where(HealthLabsNutritionWork.group_id == self.user.group_id)
                    .group_by(HealthLabsNutritionWork.state)
                ).all()
            )
        )
        requests_today = (
            self.session.scalar(
                sa.select(HealthLabsNutritionProviderUsage.request_count).where(
                    HealthLabsNutritionProviderUsage.usage_date == get_utc_now().date()
                )
            )
            or 0
        )
        provider_enabled = self.provider_daily_request_limit > 0 and self._provider_available()
        return HealthLabsNutritionWorkerStatus(
            counts=counts,
            provider_enabled=provider_enabled,
            provider_budget_exhausted=(provider_enabled and requests_today >= self.provider_daily_request_limit),
            last_updated_at=self.session.scalar(
                sa.select(sa.func.max(HealthLabsNutritionWork.update_at)).where(
                    HealthLabsNutritionWork.group_id == self.user.group_id
                )
            ),
        )

    def list_work(
        self,
        *,
        state: HealthLabsNutritionWorkState,
        kind: HealthLabsNutritionWorkKind | None,
        limit: int,
    ) -> list[HealthLabsNutritionWorkOut]:
        self._require_write()
        query = sa.select(HealthLabsNutritionWork).where(
            HealthLabsNutritionWork.group_id == self.user.group_id,
            HealthLabsNutritionWork.state == state.value,
        )
        if kind is not None:
            query = query.where(HealthLabsNutritionWork.kind == kind.value)
        rows = self.session.scalars(
            query.order_by(HealthLabsNutritionWork.kind, HealthLabsNutritionWork.target_id).limit(limit)
        ).all()
        return [self._out(row) for row in rows]

    def _provider_available(self) -> bool:
        settings = self.session.scalar(
            sa.select(AIProviderSettings).where(AIProviderSettings.group_id == self.user.group_id)
        )
        if settings is None or settings.default_provider_id is None:
            return False
        provider = self.session.get(AIProvider, settings.default_provider_id)
        return provider is not None and provider.settings_id == settings.id

    def _require_write(self) -> None:
        self.policy.require(HealthLabsCapability.NUTRITION_WRITE)

    @staticmethod
    def _out(work: HealthLabsNutritionWork) -> HealthLabsNutritionWorkOut:
        return HealthLabsNutritionWorkOut(
            id=work.id,
            kind=work.kind,
            target_id=work.target_id,
            state=work.state,
            attempt_count=work.attempt_count,
            result=work.result,
            last_error=work.last_error,
            updated_at=work.update_at,
        )
