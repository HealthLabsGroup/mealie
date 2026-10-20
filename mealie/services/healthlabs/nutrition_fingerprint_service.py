from __future__ import annotations

import hashlib
import json
from threading import Lock
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel, RecipeIngredientModel, RecipeModel
from mealie.db.models.recipe.ingredient import IngredientFoodAliasModel, IngredientUnitAliasModel

_CATALOGUE_CACHE: dict[tuple[int, UUID], tuple[str, str]] = {}
_CATALOGUE_CACHE_LOCK = Lock()


class NutritionFingerprintService:
    def __init__(
        self,
        session: Session,
        *,
        country: str,
        provider_enabled: bool,
    ) -> None:
        self.session = session
        self.country = country
        self.provider_enabled = provider_enabled
        self._catalogue_hashes: dict[UUID, str] = {}
        self._catalogue_revisions: dict[UUID, str] = {}
        self._source_hash: str | None = None

    def invalidate_catalogue(self, group_id: UUID) -> None:
        self._catalogue_hashes.pop(group_id, None)
        self._catalogue_revisions.pop(group_id, None)
        with _CATALOGUE_CACHE_LOCK:
            _CATALOGUE_CACHE.pop((id(self.session.get_bind()), group_id), None)

    def catalogue_revision(self, group_id: UUID) -> str:
        if cached := self._catalogue_revisions.get(group_id):
            return cached
        foods = self.session.execute(
            sa.select(IngredientFoodModel.id, IngredientFoodModel.update_at)
            .where(IngredientFoodModel.group_id == group_id)
            .order_by(IngredientFoodModel.id)
        ).all()
        units = self.session.execute(
            sa.select(IngredientUnitModel.id, IngredientUnitModel.update_at)
            .where(IngredientUnitModel.group_id == group_id)
            .order_by(IngredientUnitModel.id)
        ).all()
        food_aliases = self.session.execute(
            sa.select(IngredientFoodAliasModel.id, IngredientFoodAliasModel.update_at)
            .join(IngredientFoodModel, IngredientFoodModel.id == IngredientFoodAliasModel.food_id)
            .where(IngredientFoodModel.group_id == group_id)
            .order_by(IngredientFoodAliasModel.id)
        ).all()
        unit_aliases = self.session.execute(
            sa.select(IngredientUnitAliasModel.id, IngredientUnitAliasModel.update_at)
            .join(IngredientUnitModel, IngredientUnitModel.id == IngredientUnitAliasModel.unit_id)
            .where(IngredientUnitModel.group_id == group_id)
            .order_by(IngredientUnitAliasModel.id)
        ).all()
        revision = self._hash(foods, units, food_aliases, unit_aliases)
        self._catalogue_revisions[group_id] = revision
        return revision

    def catalogue(self, group_id: UUID) -> str:
        if cached := self._catalogue_hashes.get(group_id):
            return cached
        revision = self.catalogue_revision(group_id)
        cache_key = (id(self.session.get_bind()), group_id)
        with _CATALOGUE_CACHE_LOCK:
            cached_global = _CATALOGUE_CACHE.get(cache_key)
        if cached_global is not None and cached_global[0] == revision:
            self._catalogue_hashes[group_id] = cached_global[1]
            return cached_global[1]
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
        with _CATALOGUE_CACHE_LOCK:
            _CATALOGUE_CACHE[cache_key] = (revision, value)
        return value

    def ingredient(self, ingredient: RecipeIngredientModel, group_id: UUID) -> str:
        return self._hash(
            ingredient.original_text,
            ingredient.quantity,
            ingredient.note,
            ingredient.title,
            self.catalogue(group_id),
            self.provider_enabled,
            self.provider(group_id),
        )

    def provider(self, group_id: UUID) -> str:
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
            self._hash(provider.api_key),
            sorted((item.key_name or "", item.value or "") for item in provider.request_headers),
            sorted((item.key_name or "", item.value or "") for item in provider.request_params),
        )

    def food(self, food: IngredientFoodModel, match: HealthLabsFoodMatch | None) -> str:
        aliases = self.session.execute(
            sa.select(IngredientFoodAliasModel.name, IngredientFoodAliasModel.update_at)
            .where(IngredientFoodAliasModel.food_id == food.id)
            .order_by(IngredientFoodAliasModel.name)
        ).all()
        match_state = (
            (
                str(match.nutrient_food_id),
                match.country,
                match.auto_applied,
                match.reviewed_at,
                match.reviewed_by_user_id,
            )
            if match is not None
            else None
        )
        return self._hash(food.name, food.plural_name, aliases, self.country, self._source_release(), match_state)

    def portion(
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
            match.auto_applied,
            match.reviewed_at,
            source.sha256 if source is not None else None,
            portions,
        )

    def current_work_input(self, work: HealthLabsNutritionWork) -> str | None:
        if work.kind == "ingredient_structure":
            try:
                ingredient_id = int(work.target_id)
            except ValueError:
                return None
            ingredient = self.session.scalar(
                sa.select(RecipeIngredientModel)
                .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
                .where(RecipeIngredientModel.id == ingredient_id, RecipeModel.group_id == work.group_id)
            )
            return self.ingredient(ingredient, work.group_id) if ingredient is not None else None

        if work.kind == "food_match":
            try:
                food_id = UUID(work.target_id)
            except ValueError:
                return None
            food = self.session.scalar(
                sa.select(IngredientFoodModel).where(
                    IngredientFoodModel.id == food_id,
                    IngredientFoodModel.group_id == work.group_id,
                )
            )
            if food is None:
                return None
            match = self.session.scalar(
                sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
            )
            return self.food(food, match)

        if work.kind == "portion_resolution":
            try:
                ingredient_id = int(work.target_id)
            except ValueError:
                return None
            row = self.session.execute(
                sa.select(RecipeIngredientModel, HealthLabsFoodMatch)
                .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
                .join(HealthLabsFoodMatch, HealthLabsFoodMatch.ingredient_food_id == RecipeIngredientModel.food_id)
                .where(RecipeIngredientModel.id == ingredient_id, RecipeModel.group_id == work.group_id)
            ).one_or_none()
            return self.portion(row[0], row[1], work.group_id) if row is not None else None
        return None

    def _source_release(self) -> str:
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
