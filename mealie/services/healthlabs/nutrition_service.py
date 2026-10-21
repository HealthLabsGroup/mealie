from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodMatchSuppression,
    HealthLabsFoodPortion,
    HealthLabsFoodPortionMapping,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionCuratorDecision,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel
from mealie.db.models.recipe.ingredient import IngredientFoodAliasModel
from mealie.db.models.users import User
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.healthlabs.nutrition import (
    HealthLabsFoodMatchOut,
    HealthLabsFoodMatchWrite,
    HealthLabsFoodNutritionOut,
    HealthLabsFoodNutritionPage,
    HealthLabsFoodPortionCreate,
    HealthLabsFoodPortionOut,
    HealthLabsIngredientNutrition,
    HealthLabsNutrientFoodOut,
    HealthLabsNutritionSourceOut,
    HealthLabsNutritionSourceReleaseOut,
    HealthLabsNutritionStatus,
    HealthLabsNutritionValues,
    HealthLabsRecipeNutrition,
    HealthLabsReviewerOut,
)
from mealie.schema.healthlabs.nutrition_curator import public_curator_note
from mealie.schema.recipe import Recipe
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import BreakGlassContext, RecipeAccessDenied, RecipeAccessPolicy
from mealie.services.healthlabs.nutrition_fingerprint_service import NutritionFingerprintService

CORE_NUTRIENTS = ("kcal", "protein_g", "fibre_g", "carbs_g", "fat_g", "water_ml")
PUBLIC_QUANTUM = Decimal("0.001")
MASS_GRAMS = {
    "gram": Decimal("1"),
    "kilogram": Decimal("1000"),
    "ounce": Decimal("28.349523125"),
    "pound": Decimal("453.59237"),
}
VOLUME_MILLILITRES = {
    "milliliter": Decimal("1"),
    "millilitre": Decimal("1"),
    "ml": Decimal("1"),
    "liter": Decimal("1000"),
    "litre": Decimal("1000"),
    "l": Decimal("1000"),
    "fluid ounce": Decimal("29.5735295625"),
    "fluid_ounce": Decimal("29.5735295625"),
    "fl oz": Decimal("29.5735295625"),
    "cup": Decimal("236.5882365"),
}


class NutritionFoodNotFound(LookupError):
    pass


class NutritionSourceNotFound(LookupError):
    pass


class NutritionConflict(RuntimeError):
    pass


class NutritionService:
    def __init__(self, session: Session, user: PrivateUser | None = None) -> None:
        self.session = session
        self.user = user
        self.policy = RecipeAccessPolicy(session, user) if user is not None else None
        self._food_facts_cache: dict[tuple[UUID, UUID | None, float | None], HealthLabsIngredientNutrition] = {}
        self._candidate_cache: dict[
            str,
            dict[str, list[tuple[HealthLabsNutrientFood, HealthLabsNutrientSourceRelease]]],
        ] = {}
        self._match_cache: dict[UUID, HealthLabsFoodMatch | None] = {}
        self._review_attribution_cache: dict[
            UUID,
            tuple[User | None, HealthLabsNutritionCuratorDecision | None],
        ] = {}

    def list_foods(
        self,
        *,
        q: str | None,
        country: str,
        status: HealthLabsNutritionStatus | None,
    ) -> HealthLabsFoodNutritionPage:
        user = self._require_user()
        query = sa.select(IngredientFoodModel).where(IngredientFoodModel.group_id == user.group_id)
        if search := (q or "").strip():
            pattern = f"%{search.casefold()}%"
            query = query.where(
                sa.or_(
                    sa.func.lower(IngredientFoodModel.name).like(pattern),
                    sa.func.lower(IngredientFoodModel.plural_name).like(pattern),
                )
            )
        foods = self.session.scalars(query.order_by(IngredientFoodModel.name, IngredientFoodModel.id)).all()
        self._preload_review_attribution(foods, user.group_id)
        items = [self._food_out(food, country) for food in foods]
        if status is not None:
            items = [item for item in items if item.status == status]
        return HealthLabsFoodNutritionPage(items=items, total=len(items))

    def get_food(self, food_id: UUID, country: str) -> HealthLabsFoodNutritionOut:
        return self._food_out(self._get_food_for_user(food_id), country)

    def set_match(
        self,
        food_id: UUID,
        data: HealthLabsFoodMatchWrite,
        *,
        access_reason: str | None,
        break_glass_reason: str | None,
        match_method: Literal["manual", "agent"] = "manual",
        expected_input_sha256: str | None = None,
    ) -> HealthLabsFoodNutritionOut:
        food = self._get_food_for_user(food_id, for_update=True)
        country = data.country.upper()
        if match_method == "agent" and expected_input_sha256 is None:
            raise NutritionConflict("Food changed after curator evaluation")
        if match_method == "agent":
            locked = self.session.execute(
                sa.update(IngredientFoodModel)
                .where(
                    IngredientFoodModel.id == food.id,
                    IngredientFoodModel.group_id == food.group_id,
                    IngredientFoodModel.update_at == food.update_at,
                )
                .values(update_at=IngredientFoodModel.update_at)
                .execution_options(synchronize_session=False)
            ).rowcount
            if locked != 1:
                raise NutritionConflict("Food changed after curator evaluation")
            food = self._get_food_for_user(food_id, for_update=True)
        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch)
            .where(HealthLabsFoodMatch.ingredient_food_id == food_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if match_method == "agent" and (
            NutritionFingerprintService(
                self.session,
                country=country,
                provider_enabled=True,
            ).food(food, match)
            != expected_input_sha256
        ):
            raise NutritionConflict("Food changed after curator evaluation")
        self._authorize_write(food_id, "nutrition.match.set", access_reason, break_glass_reason)
        nutrient_food = self.session.get(HealthLabsNutrientFood, data.nutrient_food_id)
        if nutrient_food is None:
            raise NutritionSourceNotFound
        source = self.session.get(HealthLabsNutrientSourceRelease, nutrient_food.source_release_id)
        if source is None or source.country.upper() not in {country, "OTHER"}:
            raise NutritionSourceNotFound

        suppression = self.session.scalar(
            sa.select(HealthLabsFoodMatchSuppression).where(
                HealthLabsFoodMatchSuppression.ingredient_food_id == food_id
            )
        )
        if match_method == "agent" and suppression is not None:
            raise NutritionConflict("Suppressed matches cannot be changed by an agent")
        if match_method == "manual" and suppression is not None:
            self.session.delete(suppression)

        if match is None:
            match = HealthLabsFoodMatch(ingredient_food_id=food_id)
            self.session.add(match)
        elif match_method == "agent" and (
            match.auto_applied or (match.reviewed_by_user_id is not None and match.reviewed_at is not None)
        ):
            raise NutritionConflict("Reviewed matches cannot be changed by an agent")
        match.nutrient_food_id = nutrient_food.id
        match.country = country
        match.match_method = match_method
        match.confidence = data.confidence
        match.auto_applied = False
        if match_method == "manual":
            match.food_group = data.food_group
            match.plant_id = data.plant_id
            match.allergens = data.allergens
            match.diets = data.diets
        match.reviewed_by_user_id = self._require_user().id
        match.reviewed_at = get_utc_now()
        self.session.flush()
        self._clear_cache()
        return self._food_out(food, country)

    def clear_match(
        self,
        food_id: UUID,
        *,
        country: str,
        access_reason: str | None,
        break_glass_reason: str | None,
    ) -> HealthLabsFoodNutritionOut:
        food = self._get_food_for_user(food_id, for_update=True)
        self._authorize_write(food_id, "nutrition.match.clear", access_reason, break_glass_reason)
        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
        )
        resolved_country = match.country if match is not None else country.upper()
        suppression = self.session.scalar(
            sa.select(HealthLabsFoodMatchSuppression).where(
                HealthLabsFoodMatchSuppression.ingredient_food_id == food_id
            )
        )
        if suppression is None:
            suppression = HealthLabsFoodMatchSuppression(ingredient_food_id=food_id)
            self.session.add(suppression)
        suppression.suppressed_by_user_id = self._require_user().id
        suppression.suppressed_at = get_utc_now()
        self.session.flush()
        self.session.execute(sa.delete(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id))
        self.session.execute(
            sa.delete(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == "food_match",
                sa.func.replace(HealthLabsNutritionWork.target_id, "-", "") == food_id.hex,
            )
        )
        self.session.flush()
        self._clear_cache()
        return self._food_out(food, resolved_country)

    def add_portion(
        self,
        food_id: UUID,
        data: HealthLabsFoodPortionCreate,
        *,
        access_reason: str | None,
        break_glass_reason: str | None,
    ) -> HealthLabsFoodNutritionOut:
        food = self._get_food_for_user(food_id)
        self._authorize_write(food_id, "nutrition.portion.create", access_reason, break_glass_reason)
        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
        )
        if match is None:
            raise NutritionConflict("Food must have a reviewed match before adding portions")
        duplicate = self.session.scalar(
            sa.select(HealthLabsFoodPortion.id).where(
                HealthLabsFoodPortion.nutrient_food_id == match.nutrient_food_id,
                HealthLabsFoodPortion.group_id == food.group_id,
                HealthLabsFoodPortion.source_managed.is_(False),
                HealthLabsFoodPortion.description == data.description.strip(),
                HealthLabsFoodPortion.amount == data.amount,
                HealthLabsFoodPortion.unit == data.unit.strip(),
            )
        )
        if duplicate is not None:
            raise NutritionConflict("Food portion already exists")
        self.session.add(
            HealthLabsFoodPortion(
                nutrient_food_id=match.nutrient_food_id,
                group_id=food.group_id,
                description=data.description.strip(),
                amount=data.amount,
                unit=data.unit.strip(),
                grams=data.grams,
                source_managed=False,
            )
        )
        self.session.flush()
        self._clear_cache()
        return self._food_out(food, match.country)

    def list_sources(self) -> list[HealthLabsNutritionSourceReleaseOut]:
        self._require_user()
        sources = self.session.scalars(
            sa.select(HealthLabsNutrientSourceRelease).order_by(
                HealthLabsNutrientSourceRelease.provider,
                HealthLabsNutrientSourceRelease.version,
                HealthLabsNutrientSourceRelease.country,
            )
        ).all()
        return [
            HealthLabsNutritionSourceReleaseOut(
                id=source.id,
                provider=source.provider,
                version=source.version,
                country=source.country,
                source_url=source.source_url,
                license_url=source.license_url,
                attribution=source.attribution,
                sha256=source.sha256,
                imported_at=source.imported_at,
            )
            for source in sources
        ]

    def search_source_foods(self, query: str, country: str, limit: int) -> list[HealthLabsNutrientFoodOut]:
        self._require_user()
        normalized_query = IngredientFoodModel.normalize(query)
        terms = normalized_query.split()
        if not terms:
            return []

        country = country.upper()
        countries = [country] if country == "OTHER" else [country, "OTHER"]
        source_country = sa.func.upper(HealthLabsNutrientSourceRelease.country)
        statement = (
            sa.select(HealthLabsNutrientFood, HealthLabsNutrientSourceRelease)
            .join(
                HealthLabsNutrientSourceRelease,
                HealthLabsNutrientSourceRelease.id == HealthLabsNutrientFood.source_release_id,
            )
            .where(source_country.in_(countries))
        )
        # shortcut: scan the low-tens-of-thousands country catalogue, persist normalized names when it outgrows that.
        matches = [
            (food, source)
            for food, source in self.session.execute(statement)
            if all(term in IngredientFoodModel.normalize(food.name) for term in terms)
        ]
        measured_food_ids = set(
            self.session.scalars(
                sa.select(HealthLabsFoodPortion.nutrient_food_id).where(
                    HealthLabsFoodPortion.source_managed.is_(True),
                    HealthLabsFoodPortion.nutrient_food_id.in_([food.id for food, _ in matches]),
                )
            )
        )

        def relevance(
            item: tuple[HealthLabsNutrientFood, HealthLabsNutrientSourceRelease],
        ) -> tuple[int, int, int, int, int, str, str]:
            food, source = item
            words = IngredientFoodModel.normalize(food.name).split()
            positions: list[int] = []
            quality = 0
            for term in terms:
                exact = [
                    index
                    for index, word in enumerate(words)
                    if word == term or (len(term) > 2 and word.rstrip("s") == term.rstrip("s"))
                ]
                if exact:
                    positions.append(exact[0])
                    continue
                prefix = [index for index, word in enumerate(words) if word.startswith(term)]
                quality = max(quality, 1 if prefix else 2)
                positions.append(prefix[0] if prefix else len(words))
            return (
                0 if food.id in measured_food_ids else 1,
                quality,
                positions[0],
                0 if source.country.upper() == country else 1,
                len(words),
                food.name.casefold(),
                str(food.id),
            )

        matches.sort(key=relevance)
        return [self._source_food_out(food, source, source_only_portions=True) for food, source in matches[:limit]]

    def get_source_food(self, food_id: UUID) -> HealthLabsNutrientFoodOut:
        self._require_user()
        food = self.session.get(HealthLabsNutrientFood, food_id)
        if food is None:
            raise NutritionSourceNotFound
        source = self.session.get(HealthLabsNutrientSourceRelease, food.source_release_id)
        if source is None:
            raise NutritionSourceNotFound
        return self._source_food_out(food, source, source_only_portions=True)

    def match_food(self, food_id: UUID, country: str) -> HealthLabsFoodMatchOut | None:
        if self.session.scalar(
            sa.select(HealthLabsFoodMatchSuppression.id).where(
                HealthLabsFoodMatchSuppression.ingredient_food_id == food_id
            )
        ):
            return None
        if food_id in self._match_cache:
            existing = self._match_cache[food_id]
        else:
            existing = self.session.scalar(
                sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
            )
        if existing is not None:
            return self._match_out(existing)

        return self.suggest_food_match(food_id, country)

    def suggest_food_match(self, food_id: UUID, country: str) -> HealthLabsFoodMatchOut | None:
        food = self.session.get(IngredientFoodModel, food_id)
        if food is None or not food.name:
            return None
        primary = IngredientFoodModel.normalize(food.name)
        aliases = set(
            self.session.scalars(
                sa.select(IngredientFoodAliasModel.name_normalized).where(
                    IngredientFoodAliasModel.food_id == food_id,
                    IngredientFoodAliasModel.name_normalized.is_not(None),
                )
            )
        )
        names = {primary, *aliases}
        expected_state = self._preparation_state(food.name)
        index = self._candidate_index(country)

        for source_country in dict.fromkeys((country.upper(), "OTHER")):
            candidates: dict[UUID, tuple[HealthLabsNutrientFood, HealthLabsNutrientSourceRelease, str]] = {}
            for name in names:
                for nutrient_food, source in index.get(name, []):
                    if source.country.upper() != source_country:
                        continue
                    if (
                        expected_state
                        and nutrient_food.preparation_state
                        and expected_state != nutrient_food.preparation_state
                    ):
                        continue
                    method = "exact" if name == primary else "alias"
                    previous = candidates.get(nutrient_food.id)
                    if previous is None or method == "exact":
                        candidates[nutrient_food.id] = (nutrient_food, source, method)
            if len(candidates) > 1:
                return None
            if len(candidates) == 1:
                nutrient_food, source, method = next(iter(candidates.values()))
                return self._match_out(
                    None,
                    food_id=food_id,
                    nutrient_food=nutrient_food,
                    source=source,
                    country=country.upper(),
                    match_method=method,
                    confidence=1 if method == "exact" else 0.9,
                )
        return None

    def _food_out(self, food: IngredientFoodModel, country: str) -> HealthLabsFoodNutritionOut:
        nutrition = self.match_food(food.id, country.upper())
        return HealthLabsFoodNutritionOut(
            id=food.id,
            name=food.name,
            plural_name=food.plural_name,
            description=food.description,
            status=nutrition.status if nutrition is not None else HealthLabsNutritionStatus.UNKNOWN,
            nutrition=nutrition,
        )

    def _get_food_for_user(self, food_id: UUID, *, for_update: bool = False) -> IngredientFoodModel:
        user = self._require_user()
        statement = sa.select(IngredientFoodModel).where(
            IngredientFoodModel.id == food_id,
            IngredientFoodModel.group_id == user.group_id,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        food = self.session.scalar(statement)
        if food is None:
            raise NutritionFoodNotFound
        return food

    def _authorize_write(
        self,
        food_id: UUID,
        action: str,
        access_reason: str | None,
        break_glass_reason: str | None,
    ) -> None:
        policy = self.policy
        if policy is None:
            raise RecipeAccessDenied("Authenticated user required")
        if policy.allows(HealthLabsCapability.NUTRITION_WRITE):
            policy.record_action(
                action=action,
                reason=access_reason,
                resource_type="ingredient_food",
                resource_id=str(food_id),
            )
            return
        policy.require(
            HealthLabsCapability.NUTRITION_WRITE,
            break_glass=BreakGlassContext(
                action=action,
                reason=break_glass_reason or "",
                resource_type="ingredient_food",
                resource_id=str(food_id),
            )
            if break_glass_reason is not None
            else None,
        )

    def _require_user(self) -> PrivateUser:
        if self.user is None:
            raise RecipeAccessDenied("Authenticated user required")
        return self.user

    def _clear_cache(self) -> None:
        self._food_facts_cache.clear()
        self._candidate_cache.clear()
        self._match_cache.clear()
        self._review_attribution_cache.clear()

    def _preload_review_attribution(self, foods: list[IngredientFoodModel], group_id: UUID) -> None:
        food_ids = [food.id for food in foods]
        if not food_ids:
            return
        matches = self.session.scalars(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id.in_(food_ids))
        ).all()
        matches_by_food = {match.ingredient_food_id: match for match in matches}
        self._match_cache.update({food_id: matches_by_food.get(food_id) for food_id in food_ids})

        reviewed_matches = [match for match in matches if match.reviewed_by_user_id is not None]
        reviewer_ids = {match.reviewed_by_user_id for match in reviewed_matches}
        reviewers = {
            reviewer.id: reviewer
            for reviewer in self.session.scalars(
                sa.select(User).where(User.id.in_(reviewer_ids), User.group_id == group_id)
            )
        }
        decisions: dict[tuple[str, UUID], HealthLabsNutritionCuratorDecision] = {}
        if reviewed_matches:
            target_ids = [str(match.ingredient_food_id) for match in reviewed_matches]
            for decision in self.session.scalars(
                sa.select(HealthLabsNutritionCuratorDecision)
                .where(
                    HealthLabsNutritionCuratorDecision.group_id == group_id,
                    HealthLabsNutritionCuratorDecision.kind == "food_match",
                    HealthLabsNutritionCuratorDecision.state == "approved",
                    HealthLabsNutritionCuratorDecision.target_id.in_(target_ids),
                )
                .order_by(
                    HealthLabsNutritionCuratorDecision.update_at.desc(),
                    HealthLabsNutritionCuratorDecision.id.desc(),
                )
            ):
                decisions.setdefault((decision.target_id, decision.agent_user_id), decision)
        for match in reviewed_matches:
            reviewer = reviewers.get(match.reviewed_by_user_id)
            self._review_attribution_cache[match.ingredient_food_id] = (
                reviewer,
                decisions.get((str(match.ingredient_food_id), reviewer.id)) if reviewer is not None else None,
            )

    def food_facts(
        self,
        food_id: UUID,
        unit_id: UUID | None,
        quantity: float | None,
    ) -> HealthLabsIngredientNutrition:
        cache_key = (food_id, unit_id, quantity)
        if cached := self._food_facts_cache.get(cache_key):
            return cached

        if self.session.scalar(
            sa.select(HealthLabsFoodMatchSuppression.id).where(
                HealthLabsFoodMatchSuppression.ingredient_food_id == food_id
            )
        ):
            return self._cache_facts(
                cache_key,
                HealthLabsIngredientNutrition(status=HealthLabsNutritionStatus.UNKNOWN, missing_reason="match"),
            )

        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
        )
        if match is None:
            return self._cache_facts(
                cache_key,
                HealthLabsIngredientNutrition(status=HealthLabsNutritionStatus.UNKNOWN, missing_reason="match"),
            )

        match_out = self._match_out(match)
        if match_out.status != HealthLabsNutritionStatus.MATCHED:
            return self._cache_facts(
                cache_key,
                HealthLabsIngredientNutrition(
                    status=HealthLabsNutritionStatus.REVIEW_REQUIRED,
                    missing_reason="match_review",
                    food_match=match_out,
                ),
            )
        if quantity is None or quantity < 0:
            return self._cache_facts(
                cache_key,
                HealthLabsIngredientNutrition(
                    status=HealthLabsNutritionStatus.MATCHED,
                    missing_reason="quantity",
                    food_match=match_out,
                ),
            )

        grams = self._resolve_grams(food_id, match.nutrient_food_id, unit_id, Decimal(str(quantity)))
        if grams is None:
            return self._cache_facts(
                cache_key,
                HealthLabsIngredientNutrition(
                    status=HealthLabsNutritionStatus.MATCHED,
                    missing_reason="conversion",
                    food_match=match_out,
                ),
            )

        amounts = {
            code: self._public(self._decimal(getattr(match_out.nutrients_per_100g, code)) * grams / 100)
            if getattr(match_out.nutrients_per_100g, code) is not None
            else None
            for code in CORE_NUTRIENTS
        }
        complete = all(value is not None for value in amounts.values())
        return self._cache_facts(
            cache_key,
            HealthLabsIngredientNutrition(
                status=HealthLabsNutritionStatus.MATCHED,
                resolved_grams=self._public(grams),
                nutrients=HealthLabsNutritionValues(**amounts),
                complete=complete,
                missing_reason=None if complete else "nutrient",
                food_match=match_out,
            ),
        )

    def calculate_recipe(self, recipe: Recipe, country: str) -> HealthLabsRecipeNutrition:
        del country  # Reviewed matches already pin their source country.
        totals = {code: Decimal(0) for code in CORE_NUTRIENTS}
        known = dict.fromkeys(CORE_NUTRIENTS, True)
        missing: list[UUID] = []
        sources: dict[tuple[str, str, str], HealthLabsNutritionSourceOut] = {}
        ingredient_count = 0

        for ingredient in recipe.recipe_ingredient:
            ingredient_count += 1
            if ingredient.food is None or ingredient.food.id is None:
                missing.append(ingredient.reference_id)
                for code in CORE_NUTRIENTS:
                    known[code] = False
                continue
            facts = self.food_facts(
                ingredient.food.id,
                ingredient.unit.id if ingredient.unit is not None else None,
                ingredient.quantity,
            )
            if not facts.complete:
                missing.append(ingredient.reference_id)
            if facts.food_match is not None:
                source = facts.food_match.source
                sources[(source.provider, source.version, source.country)] = source
            grams = (
                self._resolve_grams(
                    ingredient.food.id,
                    facts.food_match.nutrient_food_id,
                    ingredient.unit.id if ingredient.unit is not None else None,
                    self._decimal(ingredient.quantity),
                )
                if (
                    facts.status == HealthLabsNutritionStatus.MATCHED
                    and facts.resolved_grams is not None
                    and facts.food_match is not None
                    and ingredient.quantity is not None
                )
                else None
            )
            for code in CORE_NUTRIENTS:
                source_amount = (
                    getattr(facts.food_match.nutrients_per_100g, code) if facts.food_match is not None else None
                )
                if grams is None or source_amount is None:
                    known[code] = False
                else:
                    totals[code] += self._decimal(source_amount) * grams / 100

        total_values = HealthLabsNutritionValues(
            **{
                code: self._public(totals[code]) if ingredient_count and known[code] else None
                for code in CORE_NUTRIENTS
            }
        )
        servings = self._decimal(recipe.recipe_servings)
        per_serving = HealthLabsNutritionValues(
            **{
                code: self._public(totals[code] / servings)
                if ingredient_count and known[code] and servings > 0
                else None
                for code in CORE_NUTRIENTS
            }
        )
        return HealthLabsRecipeNutrition(
            totals=total_values,
            per_serving=per_serving,
            complete=ingredient_count > 0 and not missing and servings > 0,
            missing_ingredient_references=missing,
            source_versions=[sources[key] for key in sorted(sources)],
        )

    def _candidate_index(
        self, country: str
    ) -> dict[str, list[tuple[HealthLabsNutrientFood, HealthLabsNutrientSourceRelease]]]:
        country = country.upper()
        if country in self._candidate_cache:
            return self._candidate_cache[country]
        countries = [country] if country == "OTHER" else [country, "OTHER"]
        rows = self.session.execute(
            sa.select(HealthLabsNutrientFood, HealthLabsNutrientSourceRelease)
            .join(
                HealthLabsNutrientSourceRelease,
                HealthLabsNutrientSourceRelease.id == HealthLabsNutrientFood.source_release_id,
            )
            .where(sa.func.upper(HealthLabsNutrientSourceRelease.country).in_(countries))
        ).all()
        index: dict[str, list[tuple[HealthLabsNutrientFood, HealthLabsNutrientSourceRelease]]] = {}
        for nutrient_food, source in rows:
            index.setdefault(IngredientFoodModel.normalize(nutrient_food.name), []).append((nutrient_food, source))
        self._candidate_cache[country] = index
        return index

    def _match_out(
        self,
        match: HealthLabsFoodMatch | None,
        *,
        food_id: UUID | None = None,
        nutrient_food: HealthLabsNutrientFood | None = None,
        source: HealthLabsNutrientSourceRelease | None = None,
        country: str | None = None,
        match_method: str | None = None,
        confidence: float | None = None,
    ) -> HealthLabsFoodMatchOut:
        if match is not None:
            food_id = match.ingredient_food_id
            nutrient_food = self.session.get(HealthLabsNutrientFood, match.nutrient_food_id)
            if nutrient_food is not None:
                source = self.session.get(HealthLabsNutrientSourceRelease, nutrient_food.source_release_id)
            country = match.country
            match_method = match.match_method
            confidence = match.confidence
        if food_id is None or nutrient_food is None or source is None or country is None or match_method is None:
            raise ValueError("Nutrition match references missing data")

        reviewed = match is not None and match.reviewed_by_user_id is not None and match.reviewed_at is not None
        auto_applied = match is not None and match.auto_applied
        group_id = self.session.scalar(sa.select(IngredientFoodModel.group_id).where(IngredientFoodModel.id == food_id))
        reviewer, decision = self._review_attribution_cache.get(food_id, (None, None))
        if reviewed and food_id not in self._review_attribution_cache and match is not None and group_id is not None:
            reviewer = self.session.scalar(
                sa.select(User).where(User.id == match.reviewed_by_user_id, User.group_id == group_id)
            )
            if reviewer is not None:
                decision = self.session.scalar(
                    sa.select(HealthLabsNutritionCuratorDecision)
                    .where(
                        HealthLabsNutritionCuratorDecision.group_id == group_id,
                        HealthLabsNutritionCuratorDecision.kind == "food_match",
                        HealthLabsNutritionCuratorDecision.target_id == str(food_id),
                        HealthLabsNutritionCuratorDecision.state == "approved",
                        HealthLabsNutritionCuratorDecision.agent_user_id == reviewer.id,
                    )
                    .order_by(
                        HealthLabsNutritionCuratorDecision.update_at.desc(),
                        HealthLabsNutritionCuratorDecision.id.desc(),
                    )
                    .limit(1)
                )
        source_food = self._source_food_out(nutrient_food, source, group_id=group_id)
        return HealthLabsFoodMatchOut(
            id=match.id if match is not None else None,
            food_id=food_id,
            nutrient_food_id=source_food.id,
            source_food_id=source_food.source_food_id,
            name=source_food.name,
            preparation_state=source_food.preparation_state,
            status=(
                HealthLabsNutritionStatus.MATCHED
                if reviewed or auto_applied
                else HealthLabsNutritionStatus.REVIEW_REQUIRED
            ),
            country=country,
            match_method=match_method,
            confidence=confidence if confidence is not None else 0,
            auto_applied=auto_applied,
            reviewed=reviewed,
            food_group=match.food_group if match is not None else None,
            plant_id=match.plant_id if match is not None else None,
            allergens=match.allergens if match is not None else None,
            diets=match.diets if match is not None else None,
            reviewed_by_user_id=match.reviewed_by_user_id if match is not None else None,
            reviewed_at=match.reviewed_at if match is not None else None,
            reviewed_by=(
                HealthLabsReviewerOut(
                    id=reviewer.id,
                    display_name=reviewer.full_name or reviewer.username or "Unknown reviewer",
                    user_type=reviewer.user_type,
                )
                if reviewer is not None
                else None
            ),
            apple_note=public_curator_note(decision.rationale) if decision is not None else None,
            nutrients_per_100g=source_food.nutrients_per_100g,
            portions=source_food.portions,
            source=source_food.source,
        )

    def _source_food_out(
        self,
        nutrient_food: HealthLabsNutrientFood,
        source: HealthLabsNutrientSourceRelease,
        *,
        source_only_portions: bool = False,
        group_id: UUID | None = None,
    ) -> HealthLabsNutrientFoodOut:
        portions_statement = sa.select(HealthLabsFoodPortion).where(
            HealthLabsFoodPortion.nutrient_food_id == nutrient_food.id
        )
        if source_only_portions:
            portions_statement = portions_statement.where(HealthLabsFoodPortion.source_managed.is_(True))
        elif group_id is not None:
            portions_statement = portions_statement.where(
                sa.or_(
                    HealthLabsFoodPortion.source_managed.is_(True),
                    HealthLabsFoodPortion.group_id == group_id,
                )
            )
        else:
            portions_statement = portions_statement.where(HealthLabsFoodPortion.source_managed.is_(True))
        portion_rows = self.session.scalars(
            portions_statement.order_by(
                HealthLabsFoodPortion.description,
                HealthLabsFoodPortion.amount,
                HealthLabsFoodPortion.unit,
                HealthLabsFoodPortion.source_managed.desc(),
                HealthLabsFoodPortion.id,
            )
        ).all()
        visible_portions: dict[tuple[str, float, str], HealthLabsFoodPortion] = {}
        for portion in portion_rows:
            visible_portions.setdefault((portion.description, portion.amount, portion.unit), portion)
        portions = visible_portions.values()
        return HealthLabsNutrientFoodOut(
            id=nutrient_food.id,
            source_food_id=nutrient_food.source_food_id,
            name=nutrient_food.name,
            preparation_state=nutrient_food.preparation_state,
            nutrients_per_100g=self._nutrition_values(nutrient_food.nutrient_values),
            portions=[
                HealthLabsFoodPortionOut(
                    description=portion.description,
                    amount=portion.amount,
                    unit=portion.unit,
                    grams=portion.grams,
                )
                for portion in portions
            ],
            source=HealthLabsNutritionSourceReleaseOut(
                id=source.id,
                provider=source.provider,
                version=source.version,
                country=source.country,
                source_url=source.source_url,
                license_url=source.license_url,
                attribution=source.attribution,
                sha256=source.sha256,
                imported_at=source.imported_at,
            ),
        )

    def _resolve_grams(
        self,
        food_id: UUID,
        nutrient_food_id: UUID,
        unit_id: UUID | None,
        quantity: Decimal,
    ) -> Decimal | None:
        if unit_id is None:
            return None
        unit = self.session.get(IngredientUnitModel, unit_id)
        if unit is None:
            return None

        group_id = self.session.scalar(sa.select(IngredientFoodModel.group_id).where(IngredientFoodModel.id == food_id))
        if group_id is not None:
            mapped_portion = self.session.scalar(
                sa.select(HealthLabsFoodPortion)
                .join(
                    HealthLabsFoodPortionMapping,
                    HealthLabsFoodPortionMapping.food_portion_id == HealthLabsFoodPortion.id,
                )
                .where(
                    HealthLabsFoodPortionMapping.group_id == group_id,
                    HealthLabsFoodPortionMapping.ingredient_food_id == food_id,
                    HealthLabsFoodPortionMapping.ingredient_unit_id == unit_id,
                    HealthLabsFoodPortion.nutrient_food_id == nutrient_food_id,
                )
            )
            if mapped_portion is not None:
                return quantity * self._decimal(mapped_portion.grams) / self._decimal(mapped_portion.amount)

        standard_quantity = self._decimal(unit.standard_quantity or 1)
        standard_unit = self._normalize_unit(unit.standard_unit or "")
        if standard_unit in MASS_GRAMS:
            return quantity * standard_quantity * MASS_GRAMS[standard_unit]

        names = {
            self._normalize_unit(value)
            for value in (unit.name, unit.plural_name, unit.abbreviation, unit.plural_abbreviation)
            if value
        }
        names.update(self._normalize_unit(alias.name) for alias in unit.aliases)
        if standard_unit:
            names.add(standard_unit)
        input_ml = (
            quantity * standard_quantity * VOLUME_MILLILITRES[standard_unit]
            if standard_unit in VOLUME_MILLILITRES
            else None
        )

        portions_statement = sa.select(HealthLabsFoodPortion).where(
            HealthLabsFoodPortion.nutrient_food_id == nutrient_food_id
        )
        if group_id is None:
            portions_statement = portions_statement.where(HealthLabsFoodPortion.source_managed.is_(True))
        else:
            portions_statement = portions_statement.where(
                sa.or_(
                    HealthLabsFoodPortion.source_managed.is_(True),
                    HealthLabsFoodPortion.group_id == group_id,
                )
            )
        portions = self.session.scalars(
            portions_statement.order_by(HealthLabsFoodPortion.source_managed.desc(), HealthLabsFoodPortion.id)
        ).all()
        for portion in portions:
            portion_unit = self._normalize_unit(portion.unit)
            portion_description = self._normalize_unit(portion.description)
            if input_ml is not None and portion_unit in VOLUME_MILLILITRES:
                portion_ml = self._decimal(portion.amount) * VOLUME_MILLILITRES[portion_unit]
                return input_ml * self._decimal(portion.grams) / portion_ml
            if portion_unit in names or portion_description in names:
                return quantity * self._decimal(portion.grams) / self._decimal(portion.amount)
        return None

    @staticmethod
    def _nutrition_values(values: dict) -> HealthLabsNutritionValues:
        return HealthLabsNutritionValues(
            **{
                code: float(amount)
                if (amount := NutritionService._source_amount(values.get(code))) is not None
                else None
                for code in CORE_NUTRIENTS
            }
        )

    @staticmethod
    def _source_amount(value: object) -> Decimal | None:
        if not isinstance(value, dict):
            return None
        return NutritionService._optional_decimal(value.get("amount"))

    @staticmethod
    def _optional_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            amount = Decimal(str(value))
        except InvalidOperation, ValueError:
            return None
        return amount if amount.is_finite() and amount >= 0 else None

    @staticmethod
    def _decimal(value: object) -> Decimal:
        return Decimal(str(value))

    @staticmethod
    def _public(value: Decimal) -> float:
        return float(value.quantize(PUBLIC_QUANTUM, rounding=ROUND_HALF_UP))

    @staticmethod
    def _normalize_unit(value: str) -> str:
        return IngredientUnitModel.normalize(value).replace("-", " ")

    @staticmethod
    def _preparation_state(name: str) -> str | None:
        normalized = name.casefold()
        if any(word in normalized for word in ("cooked", "boiled", "cuite", "cuit", "gegart", "gekocht")):
            return "cooked"
        if any(word in normalized for word in ("raw", "crue", "cru", "roh")):
            return "raw"
        if any(word in normalized for word in ("dried", "dry", "séch", "getrocknet")):
            return "dried"
        return None

    def _cache_facts(
        self,
        key: tuple[UUID, UUID | None, float | None],
        value: HealthLabsIngredientNutrition,
    ) -> HealthLabsIngredientNutrition:
        self._food_facts_cache[key] = value
        return value
