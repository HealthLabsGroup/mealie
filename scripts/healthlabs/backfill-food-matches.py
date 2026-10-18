#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

import sqlalchemy as sa
from sqlalchemy.orm import Session

import mealie.db.models._all_models  # noqa: F401
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.db.models.recipe import IngredientFoodModel
from mealie.db.models.recipe.ingredient import IngredientFoodAliasModel
from mealie.services.healthlabs.nutrition_service import NutritionService


def backfill_food_matches(session: Session, *, country: str, batch_size: int = 500) -> dict[str, int]:
    country = country.strip().upper()
    if not country or batch_size < 1:
        raise ValueError("country and a positive batch size are required")

    source_countries = [country] if country == "OTHER" else [country, "OTHER"]
    source_rows = session.execute(
        sa.select(HealthLabsNutrientFood, HealthLabsNutrientSourceRelease)
        .join(
            HealthLabsNutrientSourceRelease,
            HealthLabsNutrientSourceRelease.id == HealthLabsNutrientFood.source_release_id,
        )
        .where(sa.func.upper(HealthLabsNutrientSourceRelease.country).in_(source_countries))
    ).all()
    source_index: dict[
        str,
        list[tuple[HealthLabsNutrientFood, HealthLabsNutrientSourceRelease]],
    ] = defaultdict(list)
    for nutrient_food, release in source_rows:
        source_index[IngredientFoodModel.normalize(nutrient_food.name)].append((nutrient_food, release))

    counts = {"total": 0, "matched": 0, "review_required": 0, "unknown": 0, "created": 0}
    offset = 0
    while foods := session.scalars(
        sa.select(IngredientFoodModel).order_by(IngredientFoodModel.id).offset(offset).limit(batch_size)
    ).all():
        food_ids = [food.id for food in foods]
        existing = {
            match.ingredient_food_id: match
            for match in session.scalars(
                sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id.in_(food_ids))
            )
        }
        aliases: dict[object, set[str]] = defaultdict(set)
        for food_id, name in session.execute(
            sa.select(IngredientFoodAliasModel.food_id, IngredientFoodAliasModel.name_normalized).where(
                IngredientFoodAliasModel.food_id.in_(food_ids),
                IngredientFoodAliasModel.name_normalized.is_not(None),
            )
        ):
            aliases[food_id].add(name)

        for food in foods:
            counts["total"] += 1
            if match := existing.get(food.id):
                key = (
                    "matched"
                    if match.auto_applied or (match.reviewed_at is not None and match.reviewed_by_user_id is not None)
                    else "review_required"
                )
                counts[key] += 1
                continue
            candidate = _candidate(food, aliases[food.id], source_index, country)
            if candidate is None:
                counts["unknown"] += 1
                continue
            nutrient_food, method, confidence = candidate
            session.add(
                HealthLabsFoodMatch(
                    ingredient_food_id=food.id,
                    nutrient_food_id=nutrient_food.id,
                    country=country,
                    match_method=method,
                    confidence=confidence,
                )
            )
            counts["review_required"] += 1
            counts["created"] += 1
        session.commit()
        offset += len(foods)
    return counts


def _candidate(
    food: IngredientFoodModel,
    aliases: set[str],
    source_index: dict[str, list[tuple[HealthLabsNutrientFood, HealthLabsNutrientSourceRelease]]],
    country: str,
) -> tuple[HealthLabsNutrientFood, str, float] | None:
    if not food.name:
        return None
    primary = IngredientFoodModel.normalize(food.name)
    expected_state = NutritionService._preparation_state(food.name)
    for source_country in dict.fromkeys((country, "OTHER")):
        candidates: dict[object, tuple[HealthLabsNutrientFood, str]] = {}
        for name in {primary, *aliases}:
            for nutrient_food, release in source_index.get(name, []):
                if release.country.upper() != source_country:
                    continue
                if (
                    expected_state
                    and nutrient_food.preparation_state
                    and expected_state != nutrient_food.preparation_state
                ):
                    continue
                method = "exact" if name == primary else "alias"
                current = candidates.get(nutrient_food.id)
                if current is None or method == "exact":
                    candidates[nutrient_food.id] = (nutrient_food, method)
        if len(candidates) > 1:
            return None
        if len(candidates) == 1:
            nutrient_food, method = next(iter(candidates.values()))
            return nutrient_food, method, 1 if method == "exact" else 0.9
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Create review-required matches for exact Mealie food candidates")
    parser.add_argument("--country", required=True, help="Preferred ISO country code; OTHER remains a fallback")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    from mealie.db.db_setup import session_context

    with session_context() as session:
        report = backfill_food_matches(session, country=args.country, batch_size=args.batch_size)
    sys.stdout.write(f"{json.dumps(report, sort_keys=True)}\n")


if __name__ == "__main__":
    main()
