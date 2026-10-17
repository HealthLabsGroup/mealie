from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsNutritionProviderUsage,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel, RecipeIngredientModel, RecipeModel
from mealie.lang.providers import get_locale_provider
from mealie.schema.recipe import IngredientFood, IngredientUnit, RecipeIngredient
from mealie.services.healthlabs.ingredient_structure_service import IngredientStructureService
from mealie.services.healthlabs.nutrition_fingerprint_service import NutritionFingerprintService
from mealie.services.healthlabs.nutrition_service import NutritionService
from mealie.services.parser_services.openai.parser import OpenAIParser

RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30))
LEASE_DURATION = timedelta(minutes=15)
PROVIDER_CALL_TIMEOUT_SECONDS = 14 * 60


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
        self._fixed_now = now
        self.now = now or get_utc_now()
        self.fingerprints = NutritionFingerprintService(
            session,
            country=country,
            provider_enabled=provider_daily_request_limit > 0,
        )

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
        base, remainder = divmod(self.batch_size, 3)
        limits = [base] * 3
        start = int(self.now.timestamp() // (5 * 60)) % 3
        for index in range(remainder):
            limits[(start + index) % 3] += 1
        return sum(
            discover(limit)
            for discover, limit in zip(
                (self._discover_ingredients, self._discover_foods, self._discover_portions),
                limits,
                strict=True,
            )
            if limit
        )

    def _discover_ingredients(self, limit: int) -> int:
        changed = 0
        work_row = aliased(HealthLabsNutritionWork)
        statement = (
            sa.select(RecipeIngredientModel, RecipeModel.group_id, work_row)
            .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
            .outerjoin(
                work_row,
                sa.and_(
                    work_row.kind == "ingredient_structure",
                    work_row.target_id == sa.cast(RecipeIngredientModel.id, sa.String),
                ),
            )
            .where(
                RecipeIngredientModel.food_id.is_(None),
                RecipeIngredientModel.unit_id.is_(None),
                sa.or_(RecipeIngredientModel.quantity.is_(None), RecipeIngredientModel.quantity == 0),
                RecipeIngredientModel.referenced_recipe_id.is_(None),
                RecipeIngredientModel.title.is_(None),
                ~RecipeIngredientModel.substitutions.any(),
                sa.or_(
                    sa.func.trim(RecipeIngredientModel.original_text) != "",
                    sa.func.trim(RecipeIngredientModel.note) != "",
                ),
            )
        )
        rows = self.session.execute(
            statement.order_by(
                sa.func.coalesce(work_row.update_at, RecipeIngredientModel.update_at),
                RecipeIngredientModel.id,
            ).limit(self.batch_size)
        ).all()
        for ingredient, group_id, work in rows:
            fingerprint = self.ingredient_fingerprint(ingredient, group_id)
            changed += self._upsert(
                "ingredient_structure",
                str(ingredient.id),
                group_id,
                fingerprint,
                work=work,
            )
            if changed == limit:
                break
        return changed

    def _discover_foods(self, limit: int) -> int:
        changed = 0
        work_row = aliased(HealthLabsNutritionWork)
        target_id = sa.func.replace(sa.cast(IngredientFoodModel.id, sa.String), "-", "")
        statement = (
            sa.select(IngredientFoodModel, work_row, HealthLabsFoodMatch)
            .outerjoin(
                work_row,
                sa.and_(
                    work_row.kind == "food_match",
                    sa.func.replace(work_row.target_id, "-", "") == target_id,
                ),
            )
            .outerjoin(HealthLabsFoodMatch, HealthLabsFoodMatch.ingredient_food_id == IngredientFoodModel.id)
            .where(sa.or_(work_row.id.is_not(None), HealthLabsFoodMatch.id.is_(None)))
        )
        rows = self.session.execute(
            statement.order_by(
                sa.func.coalesce(work_row.update_at, IngredientFoodModel.update_at),
                IngredientFoodModel.id,
            ).limit(self.batch_size)
        ).all()
        for food, work, match in rows:
            target_id = str(food.id)
            fingerprint = self.fingerprints.food(food, match)
            changed += self._upsert("food_match", target_id, food.group_id, fingerprint, work=work)
            if changed == limit:
                break
        return changed

    def _discover_portions(self, limit: int) -> int:
        changed = 0
        work_row = aliased(HealthLabsNutritionWork)
        statement = (
            sa.select(RecipeIngredientModel, RecipeModel.group_id, HealthLabsFoodMatch, work_row)
            .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
            .join(HealthLabsFoodMatch, HealthLabsFoodMatch.ingredient_food_id == RecipeIngredientModel.food_id)
            .outerjoin(
                work_row,
                sa.and_(
                    work_row.kind == "portion_resolution",
                    work_row.target_id == sa.cast(RecipeIngredientModel.id, sa.String),
                ),
            )
            .where(
                RecipeIngredientModel.food_id.is_not(None),
                RecipeIngredientModel.unit_id.is_not(None),
                RecipeIngredientModel.quantity.is_not(None),
                RecipeIngredientModel.quantity > 0,
                HealthLabsFoodMatch.reviewed_by_user_id.is_not(None),
                HealthLabsFoodMatch.reviewed_at.is_not(None),
            )
        )
        rows = self.session.execute(
            statement.order_by(
                sa.func.coalesce(work_row.update_at, RecipeIngredientModel.update_at),
                RecipeIngredientModel.id,
            ).limit(self.batch_size)
        ).all()
        for ingredient, group_id, match, work in rows:
            target_id = str(ingredient.id)
            fingerprint = self.fingerprints.portion(ingredient, match, group_id)
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
        preserve_active_claim: bool = True,
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
            work.update_at = get_utc_now()
            return 0
        if (
            preserve_active_claim
            and work.state == "running"
            and work.lease_expires_at is not None
            and work.lease_expires_at > self._current_time()
        ):
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
        claim_now = self._current_time()
        due = sa.and_(
            HealthLabsNutritionWork.id == work_id,
            HealthLabsNutritionWork.next_attempt_at <= claim_now,
            sa.or_(
                HealthLabsNutritionWork.state == "pending",
                sa.and_(
                    HealthLabsNutritionWork.state == "running",
                    HealthLabsNutritionWork.lease_expires_at <= claim_now,
                ),
            ),
        )
        claimed = self.session.execute(
            sa.update(HealthLabsNutritionWork)
            .where(due)
            .values(state="running", lease_expires_at=claim_now + LEASE_DURATION)
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
        if ingredient is None or not self._ingredient_needs_structure(ingredient):
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
            request_now = self._current_time()
            work.state = "pending"
            work.next_attempt_at = datetime.combine(request_now.date() + timedelta(days=1), time.min, tzinfo=UTC)
            work.result = {"reason": "provider_budget_exhausted"}
            return

        source = (ingredient.original_text or "").strip() or (ingredient.note or "").strip()
        parser = OpenAIParser(work.group_id, self.session, get_locale_provider())
        parser.data_matcher = service.matcher  # type: ignore[assignment]
        async with asyncio.timeout(PROVIDER_CALL_TIMEOUT_SECONDS):
            parsed = await parser.parse_one(source)
        self.session.expire_all()
        ingredient = self._ingredient(work)
        if ingredient is None:
            self.session.delete(work)
            return
        if not self._ingredient_needs_structure(ingredient):
            self.session.delete(work)
            return
        self.fingerprints.invalidate_catalogue(work.group_id)
        current_fingerprint = self.ingredient_fingerprint(ingredient, work.group_id)
        if current_fingerprint != work.input_sha256:
            self._requeue_ingredient(work, ingredient, current_fingerprint)
            return
        service = IngredientStructureService(self.session, work.group_id, get_locale_provider())
        if service.apply_exact(value, parsed.ingredient):
            expected = (ingredient.original_text, ingredient.quantity, ingredient.note, ingredient.title)
            if self._apply_provider_ingredient(ingredient, value, expected):
                work.state = "completed"
                work.result = None
            else:
                self._requeue_ingredient(work, ingredient)
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

    def _apply_provider_ingredient(
        self,
        ingredient: RecipeIngredientModel,
        value: RecipeIngredient,
        expected: tuple[str | None, float | None, str | None, str | None],
    ) -> bool:
        assert isinstance(value.food, IngredientFood) and value.food.id is not None
        assert isinstance(value.unit, IngredientUnit) and value.unit.id is not None
        food_is_current = (
            sa.select(IngredientFoodModel.id)
            .where(
                IngredientFoodModel.id == value.food.id,
                IngredientFoodModel.update_at.is_not_distinct_from(value.food.updated_at),
            )
            .exists()
        )
        unit_is_current = (
            sa.select(IngredientUnitModel.id)
            .where(
                IngredientUnitModel.id == value.unit.id,
                IngredientUnitModel.update_at.is_not_distinct_from(value.unit.updated_at),
            )
            .exists()
        )
        updated = self.session.execute(
            sa.update(RecipeIngredientModel)
            .where(
                RecipeIngredientModel.id == ingredient.id,
                RecipeIngredientModel.original_text.is_not_distinct_from(expected[0]),
                RecipeIngredientModel.quantity.is_not_distinct_from(expected[1]),
                RecipeIngredientModel.note.is_not_distinct_from(expected[2]),
                RecipeIngredientModel.title.is_not_distinct_from(expected[3]),
                RecipeIngredientModel.food_id.is_(None),
                RecipeIngredientModel.unit_id.is_(None),
                RecipeIngredientModel.referenced_recipe_id.is_(None),
                ~RecipeIngredientModel.substitutions.any(),
                food_is_current,
                unit_is_current,
            )
            .values(
                quantity=value.quantity,
                food_id=value.food.id,
                unit_id=value.unit.id,
                note=value.note,
                original_text=value.original_text,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if updated == 1:
            self.session.expire(ingredient)
            return True
        self.session.refresh(ingredient)
        return False

    def _requeue_ingredient(
        self,
        work: HealthLabsNutritionWork,
        ingredient: RecipeIngredientModel,
        fingerprint: str | None = None,
    ) -> None:
        if not self._ingredient_needs_structure(ingredient):
            self.session.delete(work)
            return
        if fingerprint is None:
            self.fingerprints.invalidate_catalogue(work.group_id)
            fingerprint = self.ingredient_fingerprint(ingredient, work.group_id)
        self._upsert(
            "ingredient_structure",
            str(ingredient.id),
            work.group_id,
            fingerprint,
            work=work,
            preserve_active_claim=False,
        )

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
        request_now = self._current_time()
        usage = self.session.scalar(
            sa.select(HealthLabsNutritionProviderUsage).where(
                HealthLabsNutritionProviderUsage.usage_date == request_now.date()
            )
        )
        if usage is None:
            values = {"usage_date": request_now.date(), "request_count": 0}
            dialect = self.session.get_bind().dialect.name
            if dialect == "sqlite":
                self.session.execute(
                    sqlite_insert(HealthLabsNutritionProviderUsage)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[HealthLabsNutritionProviderUsage.usage_date])
                )
            elif dialect == "postgresql":
                self.session.execute(
                    postgresql_insert(HealthLabsNutritionProviderUsage)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[HealthLabsNutritionProviderUsage.usage_date])
                )
            else:
                self.session.add(HealthLabsNutritionProviderUsage(**values))
            self.session.flush()
            usage = self.session.scalar(
                sa.select(HealthLabsNutritionProviderUsage).where(
                    HealthLabsNutritionProviderUsage.usage_date == request_now.date()
                )
            )
            if usage is None:
                raise RuntimeError("provider usage reservation was not created")
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
        work.provider_requested_at = request_now
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
        work.input_sha256 = self.fingerprints.food(food, existing)
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
            work.next_attempt_at = self._current_time() + RETRY_DELAYS[work.attempt_count - 1]
            terminal = False
        self.session.commit()
        return terminal

    def _delete_stale_work(self) -> None:
        active_lease = sa.and_(
            HealthLabsNutritionWork.state == "running",
            HealthLabsNutritionWork.lease_expires_at.is_not(None),
            HealthLabsNutritionWork.lease_expires_at > self._current_time(),
        )
        ingredient_eligible = (
            sa.select(RecipeIngredientModel.id)
            .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
            .where(
                sa.cast(RecipeIngredientModel.id, sa.String) == HealthLabsNutritionWork.target_id,
                RecipeModel.group_id == HealthLabsNutritionWork.group_id,
                RecipeIngredientModel.food_id.is_(None),
                RecipeIngredientModel.unit_id.is_(None),
                sa.or_(RecipeIngredientModel.quantity.is_(None), RecipeIngredientModel.quantity == 0),
                RecipeIngredientModel.referenced_recipe_id.is_(None),
                RecipeIngredientModel.title.is_(None),
                ~RecipeIngredientModel.substitutions.any(),
                sa.or_(
                    sa.func.trim(RecipeIngredientModel.original_text) != "",
                    sa.func.trim(RecipeIngredientModel.note) != "",
                ),
            )
            .exists()
        )
        portion_eligible = (
            sa.select(RecipeIngredientModel.id)
            .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
            .join(HealthLabsFoodMatch, HealthLabsFoodMatch.ingredient_food_id == RecipeIngredientModel.food_id)
            .where(
                sa.cast(RecipeIngredientModel.id, sa.String) == HealthLabsNutritionWork.target_id,
                RecipeModel.group_id == HealthLabsNutritionWork.group_id,
                RecipeIngredientModel.food_id.is_not(None),
                RecipeIngredientModel.unit_id.is_not(None),
                RecipeIngredientModel.quantity.is_not(None),
                RecipeIngredientModel.quantity > 0,
                HealthLabsFoodMatch.reviewed_by_user_id.is_not(None),
                HealthLabsFoodMatch.reviewed_at.is_not(None),
            )
            .exists()
        )
        food_exists = (
            sa.select(IngredientFoodModel.id)
            .where(
                sa.func.replace(sa.cast(IngredientFoodModel.id, sa.String), "-", "")
                == sa.func.replace(HealthLabsNutritionWork.target_id, "-", ""),
                IngredientFoodModel.group_id == HealthLabsNutritionWork.group_id,
            )
            .exists()
        )
        stale_ids = list(
            self.session.scalars(
                sa.select(HealthLabsNutritionWork.id)
                .where(
                    ~active_lease,
                    sa.or_(
                        sa.and_(
                            HealthLabsNutritionWork.kind == "ingredient_structure",
                            ~ingredient_eligible,
                        ),
                        sa.and_(HealthLabsNutritionWork.kind == "portion_resolution", ~portion_eligible),
                        sa.and_(HealthLabsNutritionWork.kind == "food_match", ~food_exists),
                    ),
                )
                .order_by(HealthLabsNutritionWork.update_at, HealthLabsNutritionWork.id)
                .limit(self.batch_size)
            )
        )
        if stale_ids:
            self.session.execute(
                sa.delete(HealthLabsNutritionWork)
                .where(HealthLabsNutritionWork.id.in_(stale_ids))
                .execution_options(synchronize_session=False)
            )

    def _current_time(self) -> datetime:
        return self._fixed_now or get_utc_now()

    @staticmethod
    def _ingredient_needs_structure(ingredient: RecipeIngredientModel) -> bool:
        source = (ingredient.original_text or "").strip() or (ingredient.note or "").strip()
        return bool(
            source
            and ingredient.food_id is None
            and ingredient.unit_id is None
            and ingredient.quantity in (None, 0)
            and ingredient.referenced_recipe_id is None
            and ingredient.title is None
            and not ingredient.substitutions
        )

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

    def ingredient_fingerprint(self, ingredient: RecipeIngredientModel, group_id: UUID) -> str:
        return self.fingerprints.ingredient(ingredient, group_id)

    def _provider_fingerprint(self, group_id: UUID) -> str:
        return self.fingerprints.provider(group_id)
