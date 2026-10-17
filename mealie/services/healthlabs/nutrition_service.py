from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel
from mealie.db.models.recipe.ingredient import IngredientFoodAliasModel
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
)
from mealie.schema.recipe import Recipe
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import BreakGlassContext, RecipeAccessDenied, RecipeAccessPolicy

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
        items = [self._food_out(food, country) for food in foods]
        if status is not None:
            items = [item for item in items if item.status == status]
        return HealthLabsFoodNutritionPage(items=items, total=len(items))

    def get_food(self, food_id: UUID, country: str) -> HealthLabsFoodNutritionOut:
        user = self._require_user()
        food = self.session.get(IngredientFoodModel, food_id)
        if food is None or food.group_id != user.group_id:
            raise NutritionFoodNotFound
        return self._food_out(food, country)

    def set_match(
        self,
        food_id: UUID,
        data: HealthLabsFoodMatchWrite,
        *,
        access_reason: str | None,
        break_glass_reason: str | None,
    ) -> HealthLabsFoodNutritionOut:
        self._authorize_write(food_id, "nutrition.match.set", access_reason, break_glass_reason)
        food = self._get_food_any_group(food_id)
        nutrient_food = self.session.get(HealthLabsNutrientFood, data.nutrient_food_id)
        if nutrient_food is None:
            raise NutritionSourceNotFound
        source = self.session.get(HealthLabsNutrientSourceRelease, nutrient_food.source_release_id)
        country = data.country.upper()
        if source is None or source.country.upper() not in {country, "OTHER"}:
            raise NutritionSourceNotFound

        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
        )
        if match is None:
            match = HealthLabsFoodMatch(ingredient_food_id=food_id)
            self.session.add(match)
        match.nutrient_food_id = nutrient_food.id
        match.country = country
        match.match_method = "manual"
        match.confidence = data.confidence
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
        self._authorize_write(food_id, "nutrition.match.clear", access_reason, break_glass_reason)
        food = self._get_food_any_group(food_id)
        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
        )
        resolved_country = match.country if match is not None else country.upper()
        if match is not None:
            self.session.delete(match)
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
        self._authorize_write(food_id, "nutrition.portion.create", access_reason, break_glass_reason)
        food = self._get_food_any_group(food_id)
        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
        )
        if match is None:
            raise NutritionConflict("Food must have a reviewed match before adding portions")
        duplicate = self.session.scalar(
            sa.select(HealthLabsFoodPortion.id).where(
                HealthLabsFoodPortion.nutrient_food_id == match.nutrient_food_id,
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
        terms = IngredientFoodModel.normalize(query).split()
        if not terms:
            return []

        country = country.upper()
        countries = [country] if country == "OTHER" else [country, "OTHER"]
        source_country = sa.func.upper(HealthLabsNutrientSourceRelease.country)
        source_name = sa.func.lower(HealthLabsNutrientFood.name)
        statement = (
            sa.select(HealthLabsNutrientFood, HealthLabsNutrientSourceRelease)
            .join(
                HealthLabsNutrientSourceRelease,
                HealthLabsNutrientSourceRelease.id == HealthLabsNutrientFood.source_release_id,
            )
            .where(source_country.in_(countries), *(source_name.contains(term) for term in terms))
            .order_by(
                sa.case((source_country == country, 0), else_=1),
                sa.func.length(HealthLabsNutrientFood.name),
                source_name,
                HealthLabsNutrientFood.id,
            )
            .limit(limit)
        )
        return [self._source_food_out(food, source) for food, source in self.session.execute(statement)]

    def match_food(self, food_id: UUID, country: str) -> HealthLabsFoodMatchOut | None:
        existing = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
        )
        if existing is not None:
            return self._match_out(existing)

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

    def _get_food_any_group(self, food_id: UUID) -> IngredientFoodModel:
        food = self.session.get(IngredientFoodModel, food_id)
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

    def food_facts(
        self,
        food_id: UUID,
        unit_id: UUID | None,
        quantity: float | None,
    ) -> HealthLabsIngredientNutrition:
        cache_key = (food_id, unit_id, quantity)
        if cached := self._food_facts_cache.get(cache_key):
            return cached

        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food_id)
        )
        if match is None:
            return self._cache_facts(
                cache_key,
                HealthLabsIngredientNutrition(status=HealthLabsNutritionStatus.UNKNOWN, missing_reason="match"),
            )

        match_out = self._match_out(match)
        if not match_out.reviewed:
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

        grams = self._resolve_grams(match.nutrient_food_id, unit_id, Decimal(str(quantity)))
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
        source_food = self._source_food_out(nutrient_food, source)
        return HealthLabsFoodMatchOut(
            id=match.id if match is not None else None,
            food_id=food_id,
            nutrient_food_id=source_food.id,
            source_food_id=source_food.source_food_id,
            name=source_food.name,
            preparation_state=source_food.preparation_state,
            status=HealthLabsNutritionStatus.MATCHED if reviewed else HealthLabsNutritionStatus.REVIEW_REQUIRED,
            country=country,
            match_method=match_method,
            confidence=confidence if confidence is not None else 0,
            reviewed=reviewed,
            food_group=match.food_group if match is not None else None,
            plant_id=match.plant_id if match is not None else None,
            allergens=match.allergens if match is not None else None,
            diets=match.diets if match is not None else None,
            reviewed_by_user_id=match.reviewed_by_user_id if match is not None else None,
            reviewed_at=match.reviewed_at if match is not None else None,
            nutrients_per_100g=source_food.nutrients_per_100g,
            portions=source_food.portions,
            source=source_food.source,
        )

    def _source_food_out(
        self,
        nutrient_food: HealthLabsNutrientFood,
        source: HealthLabsNutrientSourceRelease,
    ) -> HealthLabsNutrientFoodOut:
        portions = self.session.scalars(
            sa.select(HealthLabsFoodPortion)
            .where(HealthLabsFoodPortion.nutrient_food_id == nutrient_food.id)
            .order_by(HealthLabsFoodPortion.description, HealthLabsFoodPortion.id)
        ).all()
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

    def _resolve_grams(self, nutrient_food_id: UUID, unit_id: UUID | None, quantity: Decimal) -> Decimal | None:
        if unit_id is None:
            return None
        unit = self.session.get(IngredientUnitModel, unit_id)
        if unit is None:
            return None

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

        portions = self.session.scalars(
            sa.select(HealthLabsFoodPortion).where(HealthLabsFoodPortion.nutrient_food_id == nutrient_food_id)
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
