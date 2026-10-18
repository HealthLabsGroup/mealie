from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsCapabilityGrant,
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsFoodPortionMapping,
    HealthLabsNutritionCuratorDecision,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel, RecipeIngredientModel, RecipeModel
from mealie.db.models.users import User, UserType
from mealie.lang.providers import get_locale_provider
from mealie.schema.group.ai_providers import AIProviderOut
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.healthlabs.nutrition import HealthLabsFoodMatchWrite
from mealie.schema.healthlabs.nutrition_curator import (
    NutritionCuratorCandidateSet,
    NutritionCuratorProviderDecision,
)
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.ingredient_structure_service import IngredientStructureService
from mealie.services.healthlabs.nutrition_curator_candidates import NutritionCuratorCandidateService
from mealie.services.healthlabs.nutrition_curator_identity_service import (
    CURATOR_SUBJECT_PREFIX,
    NutritionCuratorIdentityService,
)
from mealie.services.healthlabs.nutrition_curator_prompt import PROMPT_VERSION, NutritionCuratorPrompt
from mealie.services.healthlabs.nutrition_fingerprint_service import NutritionFingerprintService
from mealie.services.healthlabs.nutrition_service import NutritionConflict, NutritionService

PROMPT_NAME = "healthlabs.nutrition-curator"
_LEASE = timedelta(minutes=15)
_RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30))
_USAGE_KEYS = frozenset({"input_tokens", "cached_input_tokens", "output_tokens"})


@dataclass(frozen=True, slots=True)
class StartedCuratorDecision:
    decision_id: UUID
    agent_user_id: UUID
    provider: AIProviderOut
    prompt_name: str
    prompt_payload: str
    response_schema: type[NutritionCuratorProviderDecision]


class NutritionCuratorService:
    def __init__(self, session: Session, *, country: str = "GB", now: datetime | None = None) -> None:
        self.session = session
        self.country = country.strip().upper()
        if not self.country:
            raise ValueError("country is required")
        self.now = now or datetime.now(UTC)

    def start(self, work_id: UUID) -> StartedCuratorDecision | None:
        initial = self.session.get(HealthLabsNutritionWork, work_id)
        if initial is None or initial.state != "review_required":
            return None
        agent = NutritionCuratorIdentityService(self.session).get_or_create(initial.group_id)

        work = self.session.scalar(
            sa.select(HealthLabsNutritionWork)
            .where(HealthLabsNutritionWork.id == work_id, HealthLabsNutritionWork.state == "review_required")
            .with_for_update()
        )
        if work is None or not self._agent_is_active(agent.id, work.group_id):
            return None
        provider = self._provider(work.group_id)
        if provider is None or not self._work_is_current(work):
            return None
        candidate_set = NutritionCuratorCandidateService(self.session, country=self.country).build(work, provider)
        if candidate_set is None:
            return None

        existing = self.session.scalar(
            sa.select(HealthLabsNutritionCuratorDecision).where(
                HealthLabsNutritionCuratorDecision.kind == work.kind,
                HealthLabsNutritionCuratorDecision.target_id == work.target_id,
                HealthLabsNutritionCuratorDecision.curator_sha256 == candidate_set.curator_sha256,
            )
        )
        if existing is not None:
            if self._claim_existing(existing):
                return self._started(existing, agent, provider, candidate_set)
            return None

        empty = all(not values for values in candidate_set.prompt_payload["candidates"].values())
        decision = HealthLabsNutritionCuratorDecision(
            work_id=work.id,
            group_id=work.group_id,
            kind=work.kind,
            target_id=work.target_id,
            work_input_sha256=work.input_sha256,
            curator_sha256=candidate_set.curator_sha256,
            agent_user_id=agent.id,
            state="insufficient_evidence" if empty else "running",
            attempt_count=0 if empty else 1,
            next_attempt_at=self.now,
            lease_expires_at=None if empty else self.now + _LEASE,
            candidate_snapshot=candidate_set.snapshot,
            confidence=0 if empty else None,
            runner_up_confidence=0 if empty else None,
            reason_code="insufficient_candidates" if empty else None,
            provider_id=provider.id,
            provider_name=provider.name,
            provider_model=provider.model,
            prompt_version=PROMPT_VERSION,
            requested_at=None if empty else self.now,
        )
        try:
            with self.session.begin_nested():
                self.session.add(decision)
                self.session.flush()
        except IntegrityError:
            return None
        if empty:
            return None
        return self._started(decision, agent, provider, candidate_set)

    def _claim_existing(self, decision: HealthLabsNutritionCuratorDecision) -> bool:
        claimed = self.session.execute(
            sa.update(HealthLabsNutritionCuratorDecision)
            .where(
                HealthLabsNutritionCuratorDecision.id == decision.id,
                sa.or_(
                    sa.and_(
                        HealthLabsNutritionCuratorDecision.state == "pending",
                        HealthLabsNutritionCuratorDecision.next_attempt_at <= self.now,
                    ),
                    sa.and_(
                        HealthLabsNutritionCuratorDecision.state == "running",
                        sa.or_(
                            HealthLabsNutritionCuratorDecision.lease_expires_at.is_(None),
                            HealthLabsNutritionCuratorDecision.lease_expires_at <= self.now,
                        ),
                    ),
                ),
            )
            .values(
                state="running",
                attempt_count=HealthLabsNutritionCuratorDecision.attempt_count + 1,
                requested_at=self.now,
                lease_expires_at=self.now + _LEASE,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        self.session.expire(decision)
        if claimed != 1:
            return False
        self.session.refresh(decision)
        return True

    def apply(
        self,
        decision_id: UUID,
        response: NutritionCuratorProviderDecision,
        usage: dict[str, int],
    ) -> str:
        decision = self._decision(decision_id)
        if decision is None:
            raise LookupError(f"Curator decision {decision_id} does not exist")
        if decision.state != "running":
            return decision.state

        work = self.session.get(HealthLabsNutritionWork, decision.work_id) if decision.work_id else None
        provider = self._provider(decision.group_id)
        agent = self.session.get(User, decision.agent_user_id)
        if (
            work is None
            or provider is None
            or agent is None
            or not self._agent_is_active(agent.id, decision.group_id)
            or work.kind != decision.kind
            or work.target_id != decision.target_id
            or work.input_sha256 != decision.work_input_sha256
            or not self._work_is_current(work)
        ):
            return self._finalize(decision, "insufficient_evidence", "stale_fingerprint", usage=usage)

        candidate_set = NutritionCuratorCandidateService(self.session, country=self.country).build(work, provider)
        if candidate_set is None or candidate_set.curator_sha256 != decision.curator_sha256:
            return self._finalize(decision, "insufficient_evidence", "stale_fingerprint", usage=usage)
        try:
            response.validated_for(candidate_set)
        except ValueError:
            return self._finalize(decision, "insufficient_evidence", "invalid_selection", usage=usage)

        if response.decision == "insufficient_evidence":
            return self._finalize(
                decision,
                "insufficient_evidence",
                response.reason_code,
                response=response,
                usage=usage,
            )
        if response.confidence < 0.95:
            return self._finalize(
                decision,
                "insufficient_evidence",
                "low_confidence",
                response=response,
                usage=usage,
            )
        if Decimal(str(response.confidence)) - Decimal(str(response.runner_up_confidence)) < Decimal("0.15"):
            return self._finalize(
                decision,
                "insufficient_evidence",
                "low_margin",
                response=response,
                usage=usage,
            )

        if not self._apply_selection(decision, response, candidate_set, PrivateUser.model_validate(agent)):
            return self._finalize(decision, "insufficient_evidence", "stale_fingerprint", usage=usage)
        work.state = "completed"
        work.lease_expires_at = None
        work.result = None
        work.last_error = None
        return self._finalize(decision, "approved", response.reason_code, response=response, usage=usage)

    def defer_until(self, decision_id: UUID, when: datetime, reason_code: str) -> str:
        decision = self._decision(decision_id)
        if decision is None:
            raise LookupError(f"Curator decision {decision_id} does not exist")
        if decision.state != "running":
            return decision.state
        decision.state = "pending"
        decision.attempt_count = max(0, decision.attempt_count - 1)
        decision.next_attempt_at = when
        decision.lease_expires_at = None
        decision.reason_code = reason_code[:64]
        self.session.flush()
        return decision.state

    def fail(self, decision_id: UUID, exc: Exception) -> str:
        del exc
        decision = self._decision(decision_id)
        if decision is None:
            raise LookupError(f"Curator decision {decision_id} does not exist")
        if decision.state != "running":
            return decision.state
        decision.reason_code = "provider_failure"
        decision.lease_expires_at = None
        if decision.attempt_count >= 3:
            decision.state = "failed"
        else:
            decision.state = "pending"
            decision.next_attempt_at = self.now + _RETRY_DELAYS[decision.attempt_count - 1]
        decision.rationale = None
        self.session.flush()
        return decision.state

    def _apply_selection(
        self,
        decision: HealthLabsNutritionCuratorDecision,
        response: NutritionCuratorProviderDecision,
        candidate_set: NutritionCuratorCandidateSet,
        agent: PrivateUser,
    ) -> bool:
        if decision.kind == "ingredient_structure":
            ingredient = self._ingredient(decision)
            quantity = candidate_set.prompt_payload["target"].get("quantity")
            if ingredient is None or not quantity or response.food_id is None or response.unit_id is None:
                return False
            return IngredientStructureService(self.session, decision.group_id, get_locale_provider()).apply_selected(
                ingredient,
                quantity=quantity,
                food_id=response.food_id,
                unit_id=response.unit_id,
                expected_input=(ingredient.original_text or "").strip() or (ingredient.note or "").strip(),
            )

        if decision.kind == "food_match":
            if response.nutrient_food_id is None:
                return False
            try:
                NutritionService(self.session, agent).set_match(
                    UUID(decision.target_id),
                    HealthLabsFoodMatchWrite(
                        nutrient_food_id=response.nutrient_food_id,
                        country=self.country,
                        confidence=response.confidence,
                    ),
                    access_reason=f"Nutrition curator decision {decision.id}",
                    break_glass_reason=None,
                    match_method="agent",
                )
            except LookupError, NutritionConflict, PermissionError, ValueError:
                return False
            return True

        if response.portion_id is None:
            return False
        ingredient = self._ingredient(decision)
        if ingredient is None or ingredient.food_id is None or ingredient.unit_id is None:
            return False
        food = self.session.get(IngredientFoodModel, ingredient.food_id)
        unit = self.session.get(IngredientUnitModel, ingredient.unit_id)
        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == ingredient.food_id)
        )
        portion = self.session.get(HealthLabsFoodPortion, response.portion_id)
        if (
            food is None
            or unit is None
            or match is None
            or portion is None
            or food.group_id != decision.group_id
            or unit.group_id != decision.group_id
            or portion.nutrient_food_id != match.nutrient_food_id
            or (not portion.source_managed and portion.group_id != decision.group_id)
        ):
            return False
        mapping = self.session.scalar(
            sa.select(HealthLabsFoodPortionMapping).where(
                HealthLabsFoodPortionMapping.group_id == decision.group_id,
                HealthLabsFoodPortionMapping.ingredient_food_id == food.id,
                HealthLabsFoodPortionMapping.ingredient_unit_id == unit.id,
            )
        )
        if mapping is None:
            mapping = HealthLabsFoodPortionMapping(
                group_id=decision.group_id,
                ingredient_food_id=food.id,
                ingredient_unit_id=unit.id,
            )
            self.session.add(mapping)
        mapping.food_portion_id = portion.id
        mapping.reviewed_by_user_id = agent.id
        mapping.curator_decision_id = decision.id
        self.session.flush()
        return True

    def _finalize(
        self,
        decision: HealthLabsNutritionCuratorDecision,
        state: str,
        reason_code: str,
        *,
        response: NutritionCuratorProviderDecision | None = None,
        usage: dict[str, int],
    ) -> str:
        decision.state = state
        decision.reason_code = reason_code
        decision.lease_expires_at = None
        decision.usage = {
            key: value for key, value in usage.items() if key in _USAGE_KEYS and isinstance(value, int) and value >= 0
        }
        if response is not None:
            decision.selected_ids = {
                key: str(value)
                for key in ("food_id", "unit_id", "nutrient_food_id", "portion_id")
                if (value := getattr(response, key)) is not None
            }
            decision.confidence = response.confidence
            decision.runner_up_confidence = response.runner_up_confidence
            decision.rationale = response.rationale
        self.session.flush()
        return decision.state

    def _work_is_current(self, work: HealthLabsNutritionWork) -> bool:
        current = NutritionFingerprintService(
            self.session,
            country=self.country,
            provider_enabled=True,
        ).current_work_input(work)
        return current is not None and current == work.input_sha256

    def _provider(self, group_id: UUID) -> AIProvider | None:
        settings = self.session.scalar(sa.select(AIProviderSettings).where(AIProviderSettings.group_id == group_id))
        if settings is None or settings.default_provider_id is None:
            return None
        return self.session.scalar(
            sa.select(AIProvider).where(
                AIProvider.id == settings.default_provider_id,
                AIProvider.settings_id == settings.id,
            )
        )

    def _agent_is_active(self, user_id: UUID, group_id: UUID) -> bool:
        user = self.session.get(User, user_id)
        if (
            user is None
            or user.group_id != group_id
            or user.user_type is not UserType.AGENT
            or not user.healthlabs_managed
            or user.external_subject_id != f"{CURATOR_SUBJECT_PREFIX}{group_id}"
        ):
            return False
        return (
            self.session.scalar(
                sa.select(HealthLabsCapabilityGrant.id).where(
                    HealthLabsCapabilityGrant.user_id == user_id,
                    HealthLabsCapabilityGrant.capability == HealthLabsCapability.NUTRITION_WRITE.value,
                    HealthLabsCapabilityGrant.revoked_at.is_(None),
                )
            )
            is not None
        )

    def _decision(self, decision_id: UUID) -> HealthLabsNutritionCuratorDecision | None:
        return self.session.scalar(
            sa.select(HealthLabsNutritionCuratorDecision)
            .where(HealthLabsNutritionCuratorDecision.id == decision_id)
            .with_for_update()
        )

    def _ingredient(self, decision: HealthLabsNutritionCuratorDecision) -> RecipeIngredientModel | None:
        try:
            target_id = int(decision.target_id)
        except ValueError:
            return None
        return self.session.scalar(
            sa.select(RecipeIngredientModel)
            .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
            .where(RecipeIngredientModel.id == target_id, RecipeModel.group_id == decision.group_id)
            .with_for_update()
        )

    @staticmethod
    def _started(
        decision: HealthLabsNutritionCuratorDecision,
        agent: PrivateUser,
        provider: AIProvider,
        candidate_set: NutritionCuratorCandidateSet,
    ) -> StartedCuratorDecision:
        return StartedCuratorDecision(
            decision_id=decision.id,
            agent_user_id=agent.id,
            provider=AIProviderOut.model_validate(provider),
            prompt_name=PROMPT_NAME,
            prompt_payload=NutritionCuratorPrompt.input(candidate_set),
            response_schema=NutritionCuratorProviderDecision,
        )
