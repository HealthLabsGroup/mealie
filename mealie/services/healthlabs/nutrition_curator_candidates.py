from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodMatchSuppression,
    HealthLabsFoodPortion,
    HealthLabsFoodPortionMapping,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel, RecipeIngredientModel, RecipeModel
from mealie.db.models.recipe.ingredient import IngredientFoodAliasModel, IngredientUnitAliasModel
from mealie.lang.providers import get_locale_provider
from mealie.schema.healthlabs.nutrition_curator import NutritionCuratorCandidateSet
from mealie.services.healthlabs.ingredient_structure_service import IngredientStructureService
from mealie.services.healthlabs.nutrition_curator_prompt import (
    PERSONA_VERSION,
    POLICY_VERSION,
    PROMPT_VERSION,
    NutritionCuratorPrompt,
)
from mealie.services.healthlabs.nutrition_fingerprint_service import NutritionFingerprintService
from mealie.services.healthlabs.nutrition_provider_resolution import ResolvedCuratorProvider
from mealie.services.healthlabs.nutrition_service import NutritionService

MAX_CANDIDATES = 8
_CATALOGUE_CACHE_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class _CatalogueItem:
    id: UUID
    label: str
    names: tuple[str, ...]


_CATALOGUE_CACHE: dict[
    tuple[int, UUID],
    tuple[str, tuple[_CatalogueItem, ...], tuple[_CatalogueItem, ...]],
] = {}


class NutritionCuratorCandidateService:
    def __init__(
        self,
        session: Session,
        *,
        country: str = "GB",
        fingerprints: NutritionFingerprintService | None = None,
    ) -> None:
        self.session = session
        self.country = country.strip().upper()
        if not self.country:
            raise ValueError("country is required")
        self.fingerprints = fingerprints or NutritionFingerprintService(
            session,
            country=self.country,
            provider_enabled=True,
        )

    def build(
        self,
        work: HealthLabsNutritionWork,
        provider: ResolvedCuratorProvider,
    ) -> NutritionCuratorCandidateSet | None:
        if provider.group_id != work.group_id:
            return None
        if work.kind == "ingredient_structure":
            payload = self._ingredient_payload(work)
        elif work.kind == "food_match":
            payload = self._food_payload(work)
        elif work.kind == "portion_resolution":
            payload = self._portion_payload(work)
        else:
            return None
        if payload is None:
            return None

        snapshot = {
            "candidates": {
                name: [{"id": item["id"], "label": item["label"]} for item in values]
                for name, values in payload["candidates"].items()
            }
        }
        curator_sha256, reuse_sha256 = self._fingerprints(work, provider, payload, snapshot)
        return NutritionCuratorCandidateSet(
            kind=work.kind,
            target_id=work.target_id,
            work_input_sha256=work.input_sha256,
            curator_sha256=curator_sha256,
            reuse_sha256=reuse_sha256,
            prompt_payload=payload,
            snapshot=snapshot,
        )

    def _ingredient_payload(self, work: HealthLabsNutritionWork) -> dict[str, Any] | None:
        ingredient = self._ingredient(work)
        if ingredient is None or not self._ingredient_is_eligible(ingredient):
            return None
        source = (ingredient.original_text or "").strip() or (ingredient.note or "").strip()
        suggestion = work.result.get("suggestion") if isinstance(work.result, dict) else None
        if isinstance(suggestion, dict):
            quantity = suggestion.get("quantity")
            food_value = suggestion.get("food")
            unit_value = suggestion.get("unit")
            food_query = food_value if isinstance(food_value, str) else ""
            unit_query = unit_value if isinstance(unit_value, str) else ""
        else:
            parser = IngredientStructureService(self.session, work.group_id, get_locale_provider()).parser
            try:
                parsed = parser.parse_one_sync(source).ingredient
            except IndexError, TypeError, ValueError:
                parsed = None
            quantity = parsed.quantity if parsed is not None else None
            food_query = parsed.food.name if parsed is not None and parsed.food is not None else ""
            unit_query = parsed.unit.name if parsed is not None and parsed.unit is not None else ""
        if not isinstance(quantity, int | float) or isinstance(quantity, bool) or quantity <= 0:
            foods: list[dict[str, Any]] = []
            units: list[dict[str, Any]] = []
        else:
            catalogue_foods, catalogue_units = self._catalogue(work.group_id)
            foods = self._catalogue_candidates(
                catalogue_foods,
                food_query,
                IngredientFoodModel.normalize,
            )
            units = self._catalogue_candidates(
                catalogue_units,
                unit_query,
                IngredientUnitModel.normalize,
            )
        return {
            "kind": work.kind,
            "targetId": work.target_id,
            "target": {
                "input": (ingredient.original_text or "").strip()[:500],
                "quantity": quantity,
                "foodQuery": food_query,
                "unitQuery": unit_query,
            },
            "candidates": {"foods": foods, "units": units},
        }

    def _food_payload(self, work: HealthLabsNutritionWork) -> dict[str, Any] | None:
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
        if food is None or not food.name:
            return None
        if self.session.scalar(
            sa.select(HealthLabsFoodMatchSuppression.id).where(
                HealthLabsFoodMatchSuppression.ingredient_food_id == food.id
            )
        ):
            return None
        existing = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
        )
        if existing is not None and (
            existing.auto_applied or (existing.reviewed_by_user_id is not None and existing.reviewed_at is not None)
        ):
            return None

        names = [name for name in (food.name, food.plural_name, *(alias.name for alias in food.aliases)) if name]
        expected_state = NutritionService._preparation_state(food.name)
        countries = [self.country] if self.country == "OTHER" else [self.country, "OTHER"]
        rows = self.session.execute(
            sa.select(HealthLabsNutrientFood, HealthLabsNutrientSourceRelease)
            .join(
                HealthLabsNutrientSourceRelease,
                HealthLabsNutrientSourceRelease.id == HealthLabsNutrientFood.source_release_id,
            )
            .where(sa.func.upper(HealthLabsNutrientSourceRelease.country).in_(countries))
        ).all()
        candidates = []
        for nutrient_food, release in rows:
            if (
                expected_state
                and nutrient_food.preparation_state
                and expected_state != nutrient_food.preparation_state.strip().casefold()
            ):
                continue
            score = max(
                (
                    fuzz.token_set_ratio(
                        IngredientFoodModel.normalize(name), IngredientFoodModel.normalize(nutrient_food.name)
                    )
                    for name in names
                ),
                default=0,
            )
            specificity_score = max(
                (
                    fuzz.token_sort_ratio(
                        IngredientFoodModel.normalize(name), IngredientFoodModel.normalize(nutrient_food.name)
                    )
                    for name in names
                ),
                default=0,
            )
            if score <= 0:
                continue
            candidates.append(
                {
                    "id": str(nutrient_food.id),
                    "label": nutrient_food.name,
                    "preparationState": nutrient_food.preparation_state,
                    "country": release.country.upper(),
                    "countryCompatible": True,
                    "sourceLabel": f"{release.provider} {release.version}",
                    "score": round(score, 3),
                    "specificityScore": round(specificity_score, 3),
                }
            )
        candidates.sort(
            key=lambda item: (
                -item["score"],
                -item["specificityScore"],
                0 if item["country"] == self.country else 1,
                item["label"].casefold(),
                item["id"],
            )
        )
        return {
            "kind": work.kind,
            "targetId": work.target_id,
            "target": {
                "label": food.name,
                "aliases": sorted({name for name in names if name != food.name}, key=str.casefold),
                "preparationState": expected_state,
                "country": self.country,
            },
            "candidates": {"foods": candidates[:MAX_CANDIDATES]},
        }

    def _portion_payload(self, work: HealthLabsNutritionWork) -> dict[str, Any] | None:
        ingredient = self._ingredient(work)
        if (
            ingredient is None
            or ingredient.food_id is None
            or ingredient.unit_id is None
            or ingredient.quantity is None
            or ingredient.quantity <= 0
        ):
            return None
        food = self.session.scalar(
            sa.select(IngredientFoodModel).where(
                IngredientFoodModel.id == ingredient.food_id,
                IngredientFoodModel.group_id == work.group_id,
            )
        )
        unit = self.session.scalar(
            sa.select(IngredientUnitModel).where(
                IngredientUnitModel.id == ingredient.unit_id,
                IngredientUnitModel.group_id == work.group_id,
            )
        )
        match = self.session.scalar(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == ingredient.food_id)
        )
        if food is None or unit is None or match is None:
            return None
        mapping = self.session.scalar(
            sa.select(HealthLabsFoodPortionMapping).where(
                HealthLabsFoodPortionMapping.group_id == work.group_id,
                HealthLabsFoodPortionMapping.ingredient_food_id == food.id,
                HealthLabsFoodPortionMapping.ingredient_unit_id == unit.id,
            )
        )
        if mapping is not None:
            mapped_portion = self.session.get(HealthLabsFoodPortion, mapping.food_portion_id)
            if mapped_portion is not None and mapped_portion.nutrient_food_id == match.nutrient_food_id:
                return None
        if not match.auto_applied and (match.reviewed_by_user_id is None or match.reviewed_at is None):
            return None

        unit_names = [
            name
            for name in (
                unit.name,
                unit.plural_name,
                unit.abbreviation,
                unit.plural_abbreviation,
                *(alias.name for alias in unit.aliases),
            )
            if name
        ]
        portions = self.session.scalars(
            sa.select(HealthLabsFoodPortion).where(
                HealthLabsFoodPortion.nutrient_food_id == match.nutrient_food_id,
                sa.or_(
                    HealthLabsFoodPortion.source_managed.is_(True),
                    sa.and_(
                        HealthLabsFoodPortion.source_managed.is_(False),
                        HealthLabsFoodPortion.group_id == work.group_id,
                    ),
                ),
            )
        ).all()
        candidates = []
        for portion in portions:
            score = max(
                (
                    fuzz.token_set_ratio(
                        IngredientUnitModel.normalize(name),
                        IngredientUnitModel.normalize(f"{portion.description} {portion.unit}"),
                    )
                    for name in unit_names
                ),
                default=0,
            )
            label = f"{portion.amount:g} {portion.unit} ({portion.description})"
            candidates.append(
                {
                    "id": str(portion.id),
                    "label": label,
                    "description": portion.description,
                    "amount": portion.amount,
                    "unit": portion.unit,
                    "sourceManaged": portion.source_managed,
                    "score": round(score, 3),
                }
            )
        candidates.sort(key=lambda item: (-item["score"], item["label"].casefold(), item["id"]))
        return {
            "kind": work.kind,
            "targetId": work.target_id,
            "target": {
                "quantity": ingredient.quantity,
                "foodLabel": food.name,
                "unitLabel": unit.name,
            },
            "candidates": {"portions": candidates[:MAX_CANDIDATES]},
        }

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

    @staticmethod
    def _ingredient_is_eligible(ingredient: RecipeIngredientModel) -> bool:
        return bool(
            ((ingredient.original_text or "").strip() or (ingredient.note or "").strip())
            and ingredient.food_id is None
            and ingredient.unit_id is None
            and ingredient.quantity in (None, 0)
            and ingredient.referenced_recipe_id is None
            and ingredient.title is None
            and not ingredient.substitutions
        )

    def _catalogue_candidates(
        self,
        rows: tuple[_CatalogueItem, ...],
        query: str,
        normalize: Callable[[str], str],
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        unique: dict[str, _CatalogueItem | None] = {}
        for item in rows:
            for name in item.names:
                key = normalize(name)
                current = unique.get(key)
                if key not in unique:
                    unique[key] = item
                elif current is not None and current.id != item.id:
                    unique[key] = None
        normalized_query = normalize(query)
        exact = unique.get(normalized_query)
        ambiguous_ids = {
            item.id
            for item in rows
            if unique.get(normalized_query) is None and any(normalize(name) == normalized_query for name in item.names)
        }
        scored: list[dict[str, Any]] = []
        for item in rows:
            if item.id in ambiguous_ids:
                continue
            eligible_names: list[str] = []
            for name in item.names:
                unique_item = unique.get(normalize(name))
                if unique_item is not None and unique_item.id == item.id:
                    eligible_names.append(name)
            score = max((fuzz.token_set_ratio(normalized_query, normalize(name)) for name in eligible_names), default=0)
            if score <= 0:
                continue
            scored.append(
                {
                    "id": str(item.id),
                    "label": item.label,
                    "score": round(score, 3),
                }
            )
        scored.sort(key=lambda item: (-item["score"], item["label"].casefold(), item["id"]))
        if exact is not None:
            exact_id = str(exact.id)
            scored.sort(key=lambda item: item["id"] != exact_id)
        return scored[:MAX_CANDIDATES]

    def _catalogue(self, group_id: UUID) -> tuple[tuple[_CatalogueItem, ...], tuple[_CatalogueItem, ...]]:
        revision = self.fingerprints.catalogue_revision(group_id)
        cache_key = (id(self.session.get_bind()), group_id)
        with _CATALOGUE_CACHE_LOCK:
            cached = _CATALOGUE_CACHE.get(cache_key)
        if cached is not None and cached[0] == revision:
            return cached[1], cached[2]

        food_rows = self.session.execute(
            sa.select(IngredientFoodModel.id, IngredientFoodModel.name, IngredientFoodModel.plural_name)
            .where(IngredientFoodModel.group_id == group_id)
            .order_by(IngredientFoodModel.id)
        ).all()
        food_alias_rows = self.session.execute(
            sa.select(IngredientFoodAliasModel.food_id, IngredientFoodAliasModel.name)
            .join(IngredientFoodModel, IngredientFoodModel.id == IngredientFoodAliasModel.food_id)
            .where(IngredientFoodModel.group_id == group_id)
            .order_by(IngredientFoodAliasModel.food_id, IngredientFoodAliasModel.id)
        ).all()
        unit_rows = self.session.execute(
            sa.select(
                IngredientUnitModel.id,
                IngredientUnitModel.name,
                IngredientUnitModel.plural_name,
                IngredientUnitModel.abbreviation,
                IngredientUnitModel.plural_abbreviation,
            )
            .where(IngredientUnitModel.group_id == group_id)
            .order_by(IngredientUnitModel.id)
        ).all()
        unit_alias_rows = self.session.execute(
            sa.select(IngredientUnitAliasModel.unit_id, IngredientUnitAliasModel.name)
            .join(IngredientUnitModel, IngredientUnitModel.id == IngredientUnitAliasModel.unit_id)
            .where(IngredientUnitModel.group_id == group_id)
            .order_by(IngredientUnitAliasModel.unit_id, IngredientUnitAliasModel.id)
        ).all()

        food_aliases: dict[UUID, list[str]] = {}
        for food_id, name in food_alias_rows:
            food_aliases.setdefault(food_id, []).append(name)
        unit_aliases: dict[UUID, list[str]] = {}
        for unit_id, name in unit_alias_rows:
            unit_aliases.setdefault(unit_id, []).append(name)
        foods = tuple(
            _CatalogueItem(
                id=item_id,
                label=name or plural_name or "",
                names=tuple(value for value in (name, plural_name, *food_aliases.get(item_id, ())) if value),
            )
            for item_id, name, plural_name in food_rows
        )
        units = tuple(
            _CatalogueItem(
                id=item_id,
                label=name or plural_name or "",
                names=tuple(
                    value
                    for value in (
                        name,
                        plural_name,
                        abbreviation,
                        plural_abbreviation,
                        *unit_aliases.get(item_id, ()),
                    )
                    if value
                ),
            )
            for item_id, name, plural_name, abbreviation, plural_abbreviation in unit_rows
        )
        with _CATALOGUE_CACHE_LOCK:
            _CATALOGUE_CACHE[cache_key] = (revision, foods, units)
        return foods, units

    def _fingerprints(
        self,
        work: HealthLabsNutritionWork,
        provider: ResolvedCuratorProvider,
        prompt_payload: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> tuple[str, str]:
        countries = [self.country] if self.country == "OTHER" else [self.country, "OTHER"]
        releases = self.session.execute(
            sa.select(
                HealthLabsNutrientSourceRelease.id,
                HealthLabsNutrientSourceRelease.provider,
                HealthLabsNutrientSourceRelease.version,
                HealthLabsNutrientSourceRelease.country,
                HealthLabsNutrientSourceRelease.sha256,
            )
            .where(sa.func.upper(HealthLabsNutrientSourceRelease.country).in_(countries))
            .order_by(HealthLabsNutrientSourceRelease.id)
        ).all()
        catalogue = self.fingerprints.catalogue(work.group_id)
        context = {
            "workInput": work.input_sha256,
            "catalogue": catalogue,
            "sources": [tuple(map(str, release)) for release in releases],
            "provider": provider.identity_context(),
            "personaVersion": PERSONA_VERSION,
            "promptVersion": PROMPT_VERSION,
            "policyVersion": POLICY_VERSION,
            "systemPromptSha256": hashlib.sha256(NutritionCuratorPrompt.system().encode()).hexdigest(),
            "promptPayload": prompt_payload,
            "snapshot": snapshot,
        }
        reusable_payload = {key: value for key, value in prompt_payload.items() if key != "targetId"}
        reuse_context = {**context, "promptPayload": reusable_payload}
        return self._sha256(context), self._sha256(reuse_context)

    @classmethod
    def _sha256(cls, value: Any) -> str:
        canonical = cls._canonical(value)
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def _canonical(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._canonical(child) for key, child in sorted(value.items())}
        if isinstance(value, list | tuple):
            children = [cls._canonical(child) for child in value]
            return sorted(children, key=lambda child: json.dumps(child, sort_keys=True, default=str))
        return str(value) if isinstance(value, UUID) else value
