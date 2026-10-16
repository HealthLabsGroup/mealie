#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

import mealie.db.models._all_models  # noqa: F401
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
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


def nutrition_coverage_report(session: Session, *, external_subject_id: str | None = None) -> dict[str, Any]:
    group_id = None
    if external_subject_id is not None:
        group_id = session.scalar(sa.select(User.group_id).where(User.external_subject_id == external_subject_id))
        if group_id is None:
            raise LookupError("HealthLabs subject not found")

    food_query = sa.select(IngredientFoodModel.id)
    recipe_filter = []
    if group_id is not None:
        food_query = food_query.where(IngredientFoodModel.group_id == group_id)
        recipe_filter.append(RecipeModel.group_id == group_id)
    food_ids = list(session.scalars(food_query))
    matches = {
        match.ingredient_food_id: match
        for match in session.scalars(
            sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id.in_(food_ids))
        )
    }
    food_status = Counter(_status(matches.get(food_id)) for food_id in food_ids)

    ingredient_rows = session.execute(
        sa.select(
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
        if match is None or match.reviewed_at is None:
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
    }


def _status(match: HealthLabsFoodMatch | None) -> str:
    if match is None:
        return "unknown"
    return "matched" if match.reviewed_at is not None else "review_required"


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
    args = parser.parse_args()

    from mealie.db.db_setup import session_context

    with session_context() as session:
        report = nutrition_coverage_report(session, external_subject_id=args.external_subject_id)
    sys.stdout.write(f"{json.dumps(report, sort_keys=True)}\n")


if __name__ == "__main__":
    main()
