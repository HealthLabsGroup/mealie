from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionProviderUsage,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel, RecipeIngredientModel, RecipeModel
from mealie.db.models.recipe.ingredient import IngredientFoodAliasModel, IngredientUnitAliasModel
from mealie.lang.providers import get_locale_provider
from mealie.schema.recipe import IngredientFood, IngredientUnit, RecipeIngredient
from mealie.services.healthlabs.ingredient_structure_service import IngredientStructureService
from mealie.services.healthlabs.nutrition_service import NutritionService
from mealie.services.parser_services.openai.parser import OpenAIParser

RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30))


@dataclass(frozen=True)
class NutritionWorkerReport:
    discovered: int
    processed: int
    completed: int
    review_required: int
    failed: int


class NutritionReconciliationWorker:
    def __init__(
        self,
        session: Session,
        *,
        country: str = "GB",
        batch_size: int = 20,
        provider_daily_request_limit: int = 0,
        now: datetime | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if provider_daily_request_limit < 0:
            raise ValueError("provider_daily_request_limit cannot be negative")
        if not (country := country.strip().upper()):
            raise ValueError("country is required")
        self.session = session
        self.country = country
        self.batch_size = batch_size
        self.provider_daily_request_limit = provider_daily_request_limit
        self.now = now or get_utc_now()
        self._catalogue_hashes: dict[UUID, str] = {}
        self._source_hash: str | None = None

    async def run(self) -> NutritionWorkerReport:
        discovered = self._discover()
        self.session.commit()
        processed = completed = review_required = failed = 0

        for work_id in self._due_work_ids():
            if not self._claim(work_id):
                continue
            processed += 1
            try:
                work = self.session.get(HealthLabsNutritionWork, work_id)
                if work is None:
                    continue
                await self._process(work)
                work.lease_expires_at = None
                work.last_error = None
                self.session.commit()
                if work.state == "completed":
                    completed += 1
                elif work.state == "review_required":
                    review_required += 1
            except Exception as exc:
                self.session.rollback()
                terminal = self._record_failure(work_id, exc)
                failed += int(terminal)

        return NutritionWorkerReport(
            discovered=discovered,
            processed=processed,
            completed=completed,
            review_required=review_required,
            failed=failed,
        )

    def _discover(self) -> int:
        self._delete_stale_work()
        changed = self._discover_ingredients(self.batch_size)
        if changed < self.batch_size:
            changed += self._discover_foods(self.batch_size - changed)
        if changed < self.batch_size:
            changed += self._discover_portions(self.batch_size - changed)
        return changed

    def _discover_ingredients(self, limit: int) -> int:
        changed = 0
        rows = self.session.execute(
            sa.select(RecipeIngredientModel, RecipeModel.group_id)
            .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
            .where(
                RecipeIngredientModel.food_id.is_(None),
                RecipeIngredientModel.unit_id.is_(None),
                RecipeIngredientModel.referenced_recipe_id.is_(None),
                RecipeIngredientModel.title.is_(None),
                ~RecipeIngredientModel.substitutions.any(),
                sa.or_(
                    sa.func.trim(RecipeIngredientModel.original_text) != "",
                    sa.func.trim(RecipeIngredientModel.note) != "",
                ),
            )
            .order_by(RecipeIngredientModel.id)
        )
        for ingredient, group_id in rows:
            source = (ingredient.original_text or "").strip() or (ingredient.note or "").strip()
            fingerprint = self._hash(
                source,
                self._catalogue_hash(group_id),
                self.provider_daily_request_limit > 0,
                self._provider_fingerprint(group_id),
            )
            changed += self._upsert("ingredient_structure", str(ingredient.id), group_id, fingerprint)
            if changed == limit:
                break
        return changed

    def _discover_foods(self, limit: int) -> int:
        changed = 0
        for food in self.session.scalars(sa.select(IngredientFoodModel).order_by(IngredientFoodModel.id)):
            target_id = str(food.id)
            work = self._work("food_match", target_id)
            match = self.session.scalar(
                sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
            )
            if match is not None and work is None:
                continue
            fingerprint = self._food_fingerprint(food, match)
            changed += self._upsert("food_match", target_id, food.group_id, fingerprint, work=work)
            if changed == limit:
                break
        return changed

    def _discover_portions(self, limit: int) -> int:
        changed = 0
        rows = self.session.execute(
            sa.select(RecipeIngredientModel, RecipeModel.group_id, HealthLabsFoodMatch)
            .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
            .join(HealthLabsFoodMatch, HealthLabsFoodMatch.ingredient_food_id == RecipeIngredientModel.food_id)
            .where(
                RecipeIngredientModel.food_id.is_not(None),
                RecipeIngredientModel.unit_id.is_not(None),
                RecipeIngredientModel.quantity.is_not(None),
                RecipeIngredientModel.quantity > 0,
                HealthLabsFoodMatch.reviewed_by_user_id.is_not(None),
                HealthLabsFoodMatch.reviewed_at.is_not(None),
            )
            .order_by(RecipeIngredientModel.id)
        )
        service = NutritionService(self.session)
        for ingredient, group_id, match in rows:
            target_id = str(ingredient.id)
            work = self._work("portion_resolution", target_id)
            facts = service.food_facts(ingredient.food_id, ingredient.unit_id, ingredient.quantity)
            if facts.missing_reason != "conversion" and work is None:
                continue
            fingerprint = self._portion_fingerprint(ingredient, match, group_id)
            changed += self._upsert("portion_resolution", target_id, group_id, fingerprint, work=work)
            if changed == limit:
                break
        return changed

    def _upsert(
        self,
        kind: str,
        target_id: str,
        group_id: UUID,
        fingerprint: str,
        *,
        work: HealthLabsNutritionWork | None = None,
    ) -> int:
        work = work or self._work(kind, target_id)
        if work is None:
            self.session.add(
                HealthLabsNutritionWork(
                    kind=kind,
                    target_id=target_id,
                    group_id=group_id,
                    input_sha256=fingerprint,
                    state="pending",
                    next_attempt_at=self.now,
                )
            )
            return 1
        if work.input_sha256 == fingerprint:
            return 0
        work.group_id = group_id
        work.input_sha256 = fingerprint
        work.state = "pending"
        work.attempt_count = 0
        work.next_attempt_at = self.now
        work.lease_expires_at = None
        work.provider_requested_at = None
        work.result = None
        work.last_error = None
        return 1

    def _due_work_ids(self) -> list[UUID]:
        due = sa.and_(
            HealthLabsNutritionWork.next_attempt_at <= self.now,
            sa.or_(
                HealthLabsNutritionWork.state == "pending",
                sa.and_(
                    HealthLabsNutritionWork.state == "running",
                    HealthLabsNutritionWork.lease_expires_at <= self.now,
                ),
            ),
        )
        return list(
            self.session.scalars(
                sa.select(HealthLabsNutritionWork.id)
                .where(due)
                .order_by(
                    HealthLabsNutritionWork.next_attempt_at,
                    HealthLabsNutritionWork.kind,
                    HealthLabsNutritionWork.target_id,
                )
                .limit(self.batch_size)
            )
        )

    def _claim(self, work_id: UUID) -> bool:
        due = sa.and_(
            HealthLabsNutritionWork.id == work_id,
            HealthLabsNutritionWork.next_attempt_at <= self.now,
            sa.or_(
                HealthLabsNutritionWork.state == "pending",
                sa.and_(
                    HealthLabsNutritionWork.state == "running",
                    HealthLabsNutritionWork.lease_expires_at <= self.now,
                ),
            ),
        )
        claimed = self.session.execute(
            sa.update(HealthLabsNutritionWork)
            .where(due)
            .values(state="running", lease_expires_at=self.now + timedelta(minutes=15))
        ).rowcount
        self.session.commit()
        return claimed == 1

    async def _process(self, work: HealthLabsNutritionWork) -> None:
        if work.kind == "ingredient_structure":
            await self._process_ingredient(work)
        elif work.kind == "food_match":
            self._process_food(work)
        else:
            self._process_portion(work)

    async def _process_ingredient(self, work: HealthLabsNutritionWork) -> None:
        ingredient = self._ingredient(work)
        if ingredient is None:
            self.session.delete(work)
            return
        value = RecipeIngredient(
            quantity=ingredient.quantity,
            note=ingredient.note,
            original_text=ingredient.original_text,
            title=ingredient.title,
        )
        service = IngredientStructureService(self.session, work.group_id, get_locale_provider())
        if service.structure(value):
            self._apply_ingredient(ingredient, value)
            work.state = "completed"
            work.result = None
            return
        if self.provider_daily_request_limit == 0:
            work.state = "review_required"
            work.result = {"reason": "provider_disabled"}
            return
        if not self._provider_available(work.group_id):
            work.state = "review_required"
            work.result = {"reason": "provider_unconfigured"}
            return
        if not self._reserve_provider_request(work):
            work.state = "pending"
            work.next_attempt_at = datetime.combine(self.now.date() + timedelta(days=1), time.min, tzinfo=UTC)
            work.result = {"reason": "provider_budget_exhausted"}
            return

        source = (ingredient.original_text or "").strip() or (ingredient.note or "").strip()
        parser = OpenAIParser(work.group_id, self.session, get_locale_provider())
        parser.data_matcher = service.matcher  # type: ignore[assignment]
        parsed = await parser.parse_one(source)
        if service.apply_exact(value, parsed.ingredient):
            self._apply_ingredient(ingredient, value)
            work.state = "completed"
            work.result = None
            return
        suggestion = parsed.ingredient
        work.state = "review_required"
        work.result = {
            "reason": "ingredient_suggestion",
            "suggestion": {
                "quantity": suggestion.quantity,
                "unit": suggestion.unit.name if suggestion.unit is not None else None,
                "food": suggestion.food.name if suggestion.food is not None else None,
                "note": (suggestion.note or "").strip(),
            },
        }

    @staticmethod
    def _apply_ingredient(ingredient: RecipeIngredientModel, value: RecipeIngredient) -> None:
        assert isinstance(value.food, IngredientFood) and value.food.id is not None
        assert isinstance(value.unit, IngredientUnit) and value.unit.id is not None
        ingredient.quantity = value.quantity
        ingredient.food_id = value.food.id
        ingredient.unit_id = value.unit.id
        ingredient.note = value.note
        ingredient.original_text = value.original_text

    def _provider_available(self, group_id: UUID) -> bool:
        settings = self.session.scalar(sa.select(AIProviderSettings).where(AIProviderSettings.group_id == group_id))
        if settings is None or settings.default_provider_id is None:
            return False
        return (
            self.session.scalar(
                sa.select(AIProvider.id).where(
                    AIProvider.id == settings.default_provider_id,
                    AIProvider.settings_id == settings.id,
                )
            )
            is not None
        )

    def _reserve_provider_request(self, work: HealthLabsNutritionWork) -> bool:
        usage = self.session.scalar(
            sa.select(HealthLabsNutritionProviderUsage).where(
                HealthLabsNutritionProviderUsage.usage_date == self.now.date()
            )
        )
        if usage is None:
            usage = HealthLabsNutritionProviderUsage(usage_date=self.now.date(), request_count=0)
            self.session.add(usage)
            self.session.flush()
        reserved = self.session.execute(
            sa.update(HealthLabsNutritionProviderUsage)
            .where(
                HealthLabsNutritionProviderUsage.id == usage.id,
                HealthLabsNutritionProviderUsage.request_count < self.provider_daily_request_limit,
            )
            .values(request_count=HealthLabsNutritionProviderUsage.request_count + 1)
        ).rowcount
        if reserved != 1:
            return False
        work.provider_requested_at = self.now
        self.session.commit()
        return True

    def _process_food(self, work: HealthLabsNutritionWork) -> None:
        food = self._food(work)
        if food is None:
            self.session.delete(work)
            return
        existing = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
        )
        if existing is None:
            candidate = NutritionService(self.session).match_food(food.id, self.country)
            if candidate is not None:
                existing = HealthLabsFoodMatch(
                    ingredient_food_id=food.id,
                    nutrient_food_id=candidate.nutrient_food_id,
                    country=candidate.country,
                    match_method=candidate.match_method,
                    confidence=candidate.confidence,
                )
                self.session.add(existing)
                self.session.flush()
        work.input_sha256 = self._food_fingerprint(food, existing)
        if existing is not None and existing.reviewed_at is not None and existing.reviewed_by_user_id is not None:
            work.state = "completed"
            work.result = None
        else:
            work.state = "review_required"
            work.result = {
                "reason": "source_match_review" if existing is not None else "no_unique_source_match",
                "food_id": str(food.id),
            }

    def _process_portion(self, work: HealthLabsNutritionWork) -> None:
        ingredient = self._ingredient(work)
        if ingredient is None or ingredient.food_id is None:
            self.session.delete(work)
            return
        facts = NutritionService(self.session).food_facts(
            ingredient.food_id,
            ingredient.unit_id,
            ingredient.quantity,
        )
        if facts.resolved_grams is not None:
            work.state = "completed"
            work.result = {"resolved_grams": facts.resolved_grams}
        else:
            work.state = "review_required"
            work.result = {
                "reason": "conversion_missing",
                "food_id": str(ingredient.food_id),
                "unit_id": str(ingredient.unit_id),
                "quantity": ingredient.quantity,
            }

    def _record_failure(self, work_id: UUID, exc: Exception) -> bool:
        work = self.session.get(HealthLabsNutritionWork, work_id)
        if work is None:
            return False
        work.attempt_count += 1
        work.lease_expires_at = None
        work.last_error = type(exc).__name__[:255]
        if work.attempt_count >= 3:
            work.state = "failed"
            terminal = True
        else:
            work.state = "pending"
            work.next_attempt_at = self.now + RETRY_DELAYS[work.attempt_count - 1]
            terminal = False
        self.session.commit()
        return terminal

    def _delete_stale_work(self) -> None:
        for work in self.session.scalars(sa.select(HealthLabsNutritionWork)):
            if work.kind in {"ingredient_structure", "portion_resolution"}:
                exists = self._ingredient(work) is not None
            else:
                exists = self._food(work) is not None
            if not exists:
                self.session.delete(work)

    def _ingredient(self, work: HealthLabsNutritionWork) -> RecipeIngredientModel | None:
        try:
            target_id = int(work.target_id)
        except ValueError:
            return None
        return self.session.scalar(
            sa.select(RecipeIngredientModel)
            .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
            .where(RecipeIngredientModel.id == target_id, RecipeModel.group_id == work.group_id)
        )

    def _food(self, work: HealthLabsNutritionWork) -> IngredientFoodModel | None:
        try:
            target_id = UUID(work.target_id)
        except ValueError:
            return None
        return self.session.scalar(
            sa.select(IngredientFoodModel).where(
                IngredientFoodModel.id == target_id,
                IngredientFoodModel.group_id == work.group_id,
            )
        )

    def _work(self, kind: str, target_id: str) -> HealthLabsNutritionWork | None:
        return self.session.scalar(
            sa.select(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == kind,
                HealthLabsNutritionWork.target_id == target_id,
            )
        )

    def _catalogue_hash(self, group_id: UUID) -> str:
        if cached := self._catalogue_hashes.get(group_id):
            return cached
        foods = self.session.execute(
            sa.select(
                IngredientFoodModel.id,
                IngredientFoodModel.name,
                IngredientFoodModel.plural_name,
                IngredientFoodModel.update_at,
            )
            .where(IngredientFoodModel.group_id == group_id)
            .order_by(IngredientFoodModel.id)
        ).all()
        units = self.session.execute(
            sa.select(
                IngredientUnitModel.id,
                IngredientUnitModel.name,
                IngredientUnitModel.plural_name,
                IngredientUnitModel.abbreviation,
                IngredientUnitModel.plural_abbreviation,
                IngredientUnitModel.update_at,
            )
            .where(IngredientUnitModel.group_id == group_id)
            .order_by(IngredientUnitModel.id)
        ).all()
        food_aliases = self.session.execute(
            sa.select(IngredientFoodAliasModel.food_id, IngredientFoodAliasModel.name)
            .join(IngredientFoodModel, IngredientFoodModel.id == IngredientFoodAliasModel.food_id)
            .where(IngredientFoodModel.group_id == group_id)
            .order_by(IngredientFoodAliasModel.food_id, IngredientFoodAliasModel.name)
        ).all()
        unit_aliases = self.session.execute(
            sa.select(IngredientUnitAliasModel.unit_id, IngredientUnitAliasModel.name)
            .join(IngredientUnitModel, IngredientUnitModel.id == IngredientUnitAliasModel.unit_id)
            .where(IngredientUnitModel.group_id == group_id)
            .order_by(IngredientUnitAliasModel.unit_id, IngredientUnitAliasModel.name)
        ).all()
        value = self._hash(foods, units, food_aliases, unit_aliases)
        self._catalogue_hashes[group_id] = value
        return value

    def _provider_fingerprint(self, group_id: UUID) -> str:
        settings = self.session.scalar(sa.select(AIProviderSettings).where(AIProviderSettings.group_id == group_id))
        if settings is None or settings.default_provider_id is None:
            return self._hash(None)
        provider = self.session.get(AIProvider, settings.default_provider_id)
        if provider is None or provider.settings_id != settings.id:
            return self._hash(None)
        return self._hash(
            provider.id,
            provider.base_url,
            provider.model,
            provider.timeout,
            provider.update_at,
        )

    def _food_fingerprint(
        self,
        food: IngredientFoodModel,
        match: HealthLabsFoodMatch | None,
    ) -> str:
        aliases = self.session.execute(
            sa.select(IngredientFoodAliasModel.name, IngredientFoodAliasModel.update_at)
            .where(IngredientFoodAliasModel.food_id == food.id)
            .order_by(IngredientFoodAliasModel.name)
        ).all()
        match_state = (
            (
                str(match.nutrient_food_id),
                match.country,
                match.reviewed_at,
                match.reviewed_by_user_id,
            )
            if match is not None
            else None
        )
        return self._hash(food.name, food.plural_name, aliases, self.country, self._source_release_hash(), match_state)

    def _portion_fingerprint(
        self,
        ingredient: RecipeIngredientModel,
        match: HealthLabsFoodMatch,
        group_id: UUID,
    ) -> str:
        source = self.session.scalar(
            sa.select(HealthLabsNutrientSourceRelease)
            .join(
                HealthLabsNutrientFood, HealthLabsNutrientFood.source_release_id == HealthLabsNutrientSourceRelease.id
            )
            .where(HealthLabsNutrientFood.id == match.nutrient_food_id)
        )
        portions = self.session.execute(
            sa.select(
                HealthLabsFoodPortion.description,
                HealthLabsFoodPortion.amount,
                HealthLabsFoodPortion.unit,
                HealthLabsFoodPortion.grams,
                HealthLabsFoodPortion.source_managed,
                HealthLabsFoodPortion.group_id,
                HealthLabsFoodPortion.update_at,
            )
            .where(
                HealthLabsFoodPortion.nutrient_food_id == match.nutrient_food_id,
                sa.or_(
                    HealthLabsFoodPortion.source_managed.is_(True),
                    HealthLabsFoodPortion.group_id == group_id,
                ),
            )
            .order_by(HealthLabsFoodPortion.id)
        ).all()
        return self._hash(
            ingredient.quantity,
            ingredient.unit_id,
            match.nutrient_food_id,
            match.reviewed_at,
            source.sha256 if source is not None else None,
            portions,
        )

    def _source_release_hash(self) -> str:
        if self._source_hash is None:
            countries = [self.country] if self.country == "OTHER" else [self.country, "OTHER"]
            rows = self.session.execute(
                sa.select(
                    HealthLabsNutrientSourceRelease.id,
                    HealthLabsNutrientSourceRelease.sha256,
                    HealthLabsNutrientSourceRelease.country,
                )
                .where(sa.func.upper(HealthLabsNutrientSourceRelease.country).in_(countries))
                .order_by(HealthLabsNutrientSourceRelease.id)
            ).all()
            self._source_hash = self._hash(rows)
        return self._source_hash

    @staticmethod
    def _hash(*values: object) -> str:
        payload = json.dumps(values, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()
