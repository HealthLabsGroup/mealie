#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys

import sqlalchemy as sa
from sqlalchemy.orm import Session

import mealie.db.models._all_models  # noqa: F401
from mealie.db.models.recipe import RecipeIngredientModel, RecipeModel
from mealie.lang.providers import get_locale_provider
from mealie.schema.recipe import IngredientFood, IngredientUnit, RecipeIngredient
from mealie.services.healthlabs.ingredient_structure_service import IngredientStructureService


def backfill_ingredient_structure(
    session: Session,
    *,
    apply: bool,
    batch_size: int = 500,
) -> dict[str, int | bool]:
    if batch_size < 1:
        raise ValueError("a positive batch size is required")

    counts: dict[str, int | bool] = {
        "apply": apply,
        "examined": 0,
        "candidates": 0,
        "updated": 0,
    }
    services: dict[object, IngredientStructureService] = {}
    last_id = 0
    while batch := session.execute(
        sa.select(RecipeIngredientModel, RecipeModel.group_id)
        .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
        .where(
            RecipeIngredientModel.id > last_id,
            RecipeIngredientModel.food_id.is_(None),
            RecipeIngredientModel.unit_id.is_(None),
            RecipeIngredientModel.referenced_recipe_id.is_(None),
            RecipeIngredientModel.title.is_(None),
            ~RecipeIngredientModel.substitutions.any(),
            sa.or_(
                sa.and_(
                    RecipeIngredientModel.original_text.is_not(None),
                    sa.func.trim(RecipeIngredientModel.original_text) != "",
                ),
                sa.and_(
                    RecipeIngredientModel.note.is_not(None),
                    sa.func.trim(RecipeIngredientModel.note) != "",
                ),
            ),
        )
        .order_by(RecipeIngredientModel.id)
        .limit(batch_size)
    ).all():
        for row, group_id in batch:
            counts["examined"] += 1
            service = services.get(group_id)
            if service is None:
                service = IngredientStructureService(session, group_id, get_locale_provider())
                services[group_id] = service
            ingredient = RecipeIngredient(
                quantity=0,
                note=row.note,
                original_text=row.original_text,
            )
            if not service.structure(ingredient):
                continue

            counts["candidates"] += 1
            if apply:
                row.quantity = ingredient.quantity
                assert isinstance(ingredient.unit, IngredientUnit)
                assert isinstance(ingredient.food, IngredientFood)
                row.unit_id = ingredient.unit.id
                row.food_id = ingredient.food.id
                row.note = ingredient.note
                row.original_text = ingredient.original_text
                counts["updated"] += 1

        last_id = batch[-1][0].id
        if apply:
            session.commit()

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Structure exact legacy recipe ingredients")
    parser.add_argument("--apply", action="store_true", help="Persist exact matches; default is a dry run")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    from mealie.db.db_setup import session_context

    with session_context() as session:
        report = backfill_ingredient_structure(session, apply=args.apply, batch_size=args.batch_size)
    sys.stdout.write(f"{json.dumps(report, sort_keys=True)}\n")


if __name__ == "__main__":
    main()
