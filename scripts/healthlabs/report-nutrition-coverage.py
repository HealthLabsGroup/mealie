#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

import mealie.db.models._all_models  # noqa: F401
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionCuratorDecision,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel, RecipeIngredientModel, RecipeModel
from mealie.db.models.recipe.ingredient import IngredientUnitAliasModel
from mealie.db.models.users import User
from mealie.services.healthlabs.nutrition_service import (
    CORE_NUTRIENTS,
    MASS_GRAMS,
    VOLUME_MILLILITRES,
    NutritionService,
)

GAP_PRIORITY = {"portion_resolution": 1, "food_match": 2, "ingredient_structure": 3}


def nutrition_coverage_report(
    session: Session,
    *,
    external_subject_id: str | None = None,
    country: str = "GB",
) -> dict[str, Any]:
    country = country.strip().upper()
    if not country:
        raise ValueError("country is required")
    group_id = None
    if external_subject_id is not None:
        group_id = session.scalar(sa.select(User.group_id).where(User.external_subject_id == external_subject_id))
        if group_id is None:
            raise LookupError("HealthLabs subject not found")

    food_query = sa.select(IngredientFoodModel)
    recipe_filter = []
    if group_id is not None:
        food_query = food_query.where(IngredientFoodModel.group_id == group_id)
        recipe_filter.append(RecipeModel.group_id == group_id)
    foods = list(session.scalars(food_query))
    foods_by_id = {food.id: food for food in foods}
    food_ids = list(foods_by_id)
    matches = {
        match.ingredient_food_id: match
        for match in session.scalars(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id.in_(food_ids))
        )
    }
    food_status = Counter(_status(matches.get(food_id)) for food_id in food_ids)

    ingredient_rows = session.execute(
        sa.select(
            RecipeIngredientModel.id,
            RecipeIngredientModel.recipe_id,
            RecipeIngredientModel.food_id,
            RecipeIngredientModel.unit_id,
            RecipeIngredientModel.quantity,
        )
        .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
        .where(*recipe_filter)
    ).all()
    structured = [row for row in ingredient_rows if row.food_id is not None]
    ingredient_status = Counter(_status(matches.get(row.food_id)) for row in structured)

    nutrient_food_ids = {match.nutrient_food_id for match in matches.values()}
    nutrient_foods = {
        food.id: food
        for food in session.scalars(
            sa.select(HealthLabsNutrientFood).where(HealthLabsNutrientFood.id.in_(nutrient_food_ids))
        )
    }
    unit_ids = {row.unit_id for row in structured if row.unit_id is not None}
    units = {
        unit.id: unit
        for unit in session.scalars(sa.select(IngredientUnitModel).where(IngredientUnitModel.id.in_(unit_ids)))
    }
    unit_aliases: dict[object, set[str]] = defaultdict(set)
    for unit_id, name in session.execute(
        sa.select(IngredientUnitAliasModel.unit_id, IngredientUnitAliasModel.name).where(
            IngredientUnitAliasModel.unit_id.in_(unit_ids)
        )
    ):
        unit_aliases[unit_id].add(name)
    portions: dict[object, list[HealthLabsFoodPortion]] = defaultdict(list)
    for portion in session.scalars(
        sa.select(HealthLabsFoodPortion).where(HealthLabsFoodPortion.nutrient_food_id.in_(nutrient_food_ids))
    ):
        portions[portion.nutrient_food_id].append(portion)

    missing_conversion = 0
    missing_core_nutrient = 0
    for row in structured:
        match = matches.get(row.food_id)
        if not _is_effective_match(match):
            continue
        nutrient_food = nutrient_foods.get(match.nutrient_food_id)
        if nutrient_food is None:
            continue
        if not _has_conversion(row.quantity, units.get(row.unit_id), unit_aliases, portions[match.nutrient_food_id]):
            missing_conversion += 1
        if any(
            NutritionService._source_amount(nutrient_food.nutrient_values.get(code)) is None for code in CORE_NUTRIENTS
        ):
            missing_core_nutrient += 1

    sources = list(
        session.scalars(
            sa.select(HealthLabsNutrientSourceRelease).order_by(
                HealthLabsNutrientSourceRelease.provider,
                HealthLabsNutrientSourceRelease.version,
                HealthLabsNutrientSourceRelease.country,
            )
        )
    )
    source_food_counts = Counter(
        source_release_id
        for (source_release_id,) in session.execute(sa.select(HealthLabsNutrientFood.source_release_id))
    )
    match_country_counts = Counter(match.country for match in matches.values())
    match_source_counts = Counter(
        nutrient_foods[match.nutrient_food_id].source_release_id
        for match in matches.values()
        if match.nutrient_food_id in nutrient_foods
    )

    return {
        "recipes": session.scalar(sa.select(sa.func.count()).select_from(RecipeModel).where(*recipe_filter)) or 0,
        "foods": {
            "total": len(food_ids),
            "matched": food_status["matched"],
            "review_required": food_status["review_required"],
            "unknown": food_status["unknown"],
        },
        "ingredients": {
            "total": len(ingredient_rows),
            "structured": len(structured),
            "unparsed": len(ingredient_rows) - len(structured),
            "matched": ingredient_status["matched"],
            "review_required": ingredient_status["review_required"],
            "unknown": ingredient_status["unknown"],
            "missing_conversion": missing_conversion,
            "missing_core_nutrient": missing_core_nutrient,
        },
        "matches_by_country": dict(sorted(match_country_counts.items())),
        "source_versions": [
            {
                "provider": source.provider,
                "version": source.version,
                "country": source.country,
                "sha256": source.sha256,
                "imported_foods": source_food_counts[source.id],
                "food_matches": match_source_counts[source.id],
            }
            for source in sources
        ],
        "mock_source_releases": sum(
            any(marker in source.provider.casefold() for marker in ("mock", "fixture", "synthetic"))
            for source in sources
        ),
        "gaps": _gap_statistics(
            session,
            group_id=group_id,
            country=country,
            ingredient_rows=ingredient_rows,
            foods=foods_by_id,
            units=units,
            matches=matches,
            nutrient_foods=nutrient_foods,
        ),
    }


def _gap_statistics(
    session: Session,
    *,
    group_id: object | None,
    country: str,
    ingredient_rows: list[Any],
    foods: dict[object, IngredientFoodModel],
    units: dict[object, IngredientUnitModel],
    matches: dict[object, HealthLabsFoodMatch],
    nutrient_foods: dict[object, HealthLabsNutrientFood],
) -> list[dict[str, Any]]:
    work_query = sa.select(HealthLabsNutritionWork).where(HealthLabsNutritionWork.state == "review_required")
    if group_id is not None:
        work_query = work_query.where(HealthLabsNutritionWork.group_id == group_id)
    work = list(session.scalars(work_query))
    if not work:
        return []

    rows_by_id = {str(row.id): row for row in ingredient_rows}
    recipes_by_food: dict[object, set[object]] = defaultdict(set)
    for row in ingredient_rows:
        if row.food_id is not None and row.recipe_id is not None:
            recipes_by_food[row.food_id].add(row.recipe_id)

    reason_by_work_id: dict[object, str] = {}
    decision_query = (
        sa.select(HealthLabsNutritionCuratorDecision)
        .join(HealthLabsNutritionWork, HealthLabsNutritionCuratorDecision.work_id == HealthLabsNutritionWork.id)
        .where(
            HealthLabsNutritionWork.state == "review_required",
            HealthLabsNutritionCuratorDecision.state.in_(("insufficient_evidence", "failed")),
        )
        .order_by(HealthLabsNutritionCuratorDecision.update_at.desc())
    )
    if group_id is not None:
        decision_query = decision_query.where(HealthLabsNutritionWork.group_id == group_id)
    for decision in session.scalars(decision_query):
        if decision.work_id is not None and decision.reason_code:
            reason_by_work_id.setdefault(decision.work_id, decision.reason_code)

    grouped: dict[tuple[Any, ...], dict[str, set[object]]] = defaultdict(lambda: {"recipes": set(), "targets": set()})
    for item in work:
        result = item.result if isinstance(item.result, dict) else {}
        reason_code = reason_by_work_id.get(item.id) or result.get("reason") or item.last_error or "unknown"
        normalized_food = None
        preparation_state = None
        unit_name = None
        item_country = country
        recipe_ids: set[object] = set()

        if item.kind == "food_match":
            food = _food_for_target(foods, item.target_id)
            if food is not None:
                normalized_food = food.name_normalized or _normalize_food(food.name)
                preparation_state = NutritionService._preparation_state(food.name or "")
                recipe_ids = recipes_by_food[food.id]
        else:
            ingredient = rows_by_id.get(item.target_id)
            if ingredient is not None:
                recipe_ids = {ingredient.recipe_id} if ingredient.recipe_id is not None else set()
                food = foods.get(ingredient.food_id)
                if food is not None:
                    normalized_food = food.name_normalized or _normalize_food(food.name)
                    match = matches.get(food.id)
                    source_food = nutrient_foods.get(match.nutrient_food_id) if match is not None else None
                    preparation_state = (
                        source_food.preparation_state
                        if source_food is not None and source_food.preparation_state
                        else NutritionService._preparation_state(food.name or "")
                    )
                    item_country = match.country if match is not None else country
                unit = units.get(ingredient.unit_id)
                if unit is not None:
                    unit_name = unit.name_normalized or IngredientUnitModel.normalize(unit.name or "") or None

            suggestion = result.get("suggestion") if item.kind == "ingredient_structure" else None
            if isinstance(suggestion, dict):
                normalized_food = normalized_food or _normalize_food(suggestion.get("food"))
                unit_name = unit_name or _normalize_unit(suggestion.get("unit"))
                preparation_state = preparation_state or NutritionService._preparation_state(
                    suggestion.get("food") or ""
                )

        key = (
            item.kind,
            item_country,
            normalized_food,
            preparation_state,
            unit_name,
            str(reason_code),
        )
        grouped[key]["recipes"].update(recipe_ids)
        grouped[key]["targets"].add(item.target_id)

    gaps = [
        {
            "priority": GAP_PRIORITY[kind],
            "kind": kind,
            "country": item_country,
            "normalized_food": normalized_food,
            "preparation_state": preparation_state,
            "unit": unit_name,
            "reason_code": reason_code,
            "affected_recipes": len(counts["recipes"]),
            "affected_targets": len(counts["targets"]),
        }
        for (kind, item_country, normalized_food, preparation_state, unit_name, reason_code), counts in grouped.items()
    ]
    return sorted(
        gaps,
        key=lambda row: (
            row["priority"],
            -row["affected_recipes"],
            -row["affected_targets"],
            row["country"],
            row["normalized_food"] or "",
            row["preparation_state"] or "",
            row["unit"] or "",
            row["reason_code"],
        ),
    )


def _food_for_target(foods: dict[object, IngredientFoodModel], target_id: str) -> IngredientFoodModel | None:
    try:
        return foods.get(UUID(target_id))
    except ValueError:
        return None


def _normalize_food(value: Any) -> str | None:
    return IngredientFoodModel.normalize(value) if isinstance(value, str) and value.strip() else None


def _normalize_unit(value: Any) -> str | None:
    return IngredientUnitModel.normalize(value) if isinstance(value, str) and value.strip() else None


def _status(match: HealthLabsFoodMatch | None) -> str:
    if match is None:
        return "unknown"
    return "matched" if _is_effective_match(match) else "review_required"


def _is_effective_match(match: HealthLabsFoodMatch | None) -> bool:
    return match is not None and (
        match.auto_applied or (match.reviewed_at is not None and match.reviewed_by_user_id is not None)
    )


def _has_conversion(
    quantity: float | None,
    unit: IngredientUnitModel | None,
    aliases: dict[object, set[str]],
    portions: list[HealthLabsFoodPortion],
) -> bool:
    if quantity is None or quantity < 0 or unit is None:
        return False
    standard_unit = NutritionService._normalize_unit(unit.standard_unit or "")
    if standard_unit in MASS_GRAMS:
        return True
    names = {
        NutritionService._normalize_unit(value)
        for value in (unit.name, unit.plural_name, unit.abbreviation, unit.plural_abbreviation)
        if value
    }
    names.update(NutritionService._normalize_unit(value) for value in aliases[unit.id])
    if standard_unit:
        names.add(standard_unit)
    input_ml = (
        Decimal(str(quantity)) * Decimal(str(unit.standard_quantity or 1)) * VOLUME_MILLILITRES[standard_unit]
        if standard_unit in VOLUME_MILLILITRES
        else None
    )
    for portion in portions:
        portion_unit = NutritionService._normalize_unit(portion.unit)
        if input_ml is not None and portion_unit in VOLUME_MILLILITRES:
            return True
        if portion_unit in names or NutritionService._normalize_unit(portion.description) in names:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Report aggregate HealthLabs nutrition coverage")
    parser.add_argument("--external-subject-id")
    parser.add_argument("--country", default="GB")
    args = parser.parse_args()

    from mealie.db.db_setup import session_context

    with session_context() as session:
        report = nutrition_coverage_report(
            session,
            external_subject_id=args.external_subject_id,
            country=args.country,
        )
    sys.stdout.write(f"{json.dumps(report, sort_keys=True)}\n")


if __name__ == "__main__":
    main()
