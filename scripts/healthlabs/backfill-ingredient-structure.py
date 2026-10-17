#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

import mealie.db.models._all_models  # noqa: F401
from mealie.db.models.recipe import RecipeIngredientModel, RecipeModel
from mealie.lang.providers import get_locale_provider
from mealie.schema.recipe import CreateIngredientFood, CreateIngredientUnit, IngredientFood, IngredientUnit
from mealie.schema.response.pagination import PaginationQuery
from mealie.services.parser_services.ingredient_parser import BruteForceParser


class _ExactDataMatcher:
    def __init__(self, parser: BruteForceParser) -> None:
        query = PaginationQuery(page=1, per_page=-1)
        self.foods = self._unique_map(
            parser.repos.ingredient_foods.page_all(query).items,
            lambda item: (item.name, item.plural_name, *(alias.name for alias in item.aliases or [])),
            parser.repos.ingredient_foods.model.normalize,
        )
        self.units = self._unique_map(
            parser.repos.ingredient_units.page_all(query).items,
            lambda item: (
                item.name,
                item.plural_name,
                item.abbreviation,
                item.plural_abbreviation,
                *(alias.name for alias in item.aliases or []),
            ),
            parser.repos.ingredient_units.model.normalize,
        )

    @staticmethod
    def _unique_map(items: Iterable[Any], names: Any, normalize: Any) -> dict[str, Any | None]:
        result: dict[str, Any | None] = {}
        for item in items:
            for name in names(item):
                if not name:
                    continue
                key = normalize(name)
                current = result.get(key)
                if key not in result:
                    result[key] = item
                elif current is not None and current.id != item.id:
                    result[key] = None
        return result

    def find_food_match(self, food: IngredientFood | CreateIngredientFood | str) -> IngredientFood | None:
        name = food if isinstance(food, str) else food.name
        return self.foods.get(type(self)._normalize_food(name))

    def find_unit_match(self, unit: IngredientUnit | CreateIngredientUnit | str) -> IngredientUnit | None:
        name = unit if isinstance(unit, str) else unit.name
        return self.units.get(type(self)._normalize_unit(name))

    @staticmethod
    def _normalize_food(value: str) -> str:
        from mealie.db.models.recipe import IngredientFoodModel

        return IngredientFoodModel.normalize(value)

    @staticmethod
    def _normalize_unit(value: str) -> str:
        from mealie.db.models.recipe import IngredientUnitModel

        return IngredientUnitModel.normalize(value)


async def _parse_batch(
    rows: list[tuple[RecipeIngredientModel, BruteForceParser]],
) -> list[tuple[RecipeIngredientModel, Any | None]]:
    parsed = []
    for row, parser in rows:
        try:
            parsed.append((row, await parser.parse_one(row.original_text or "")))
        except IndexError, TypeError, ValueError:
            parsed.append((row, None))
    return parsed


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
        "no_positive_quantity": 0,
        "no_exact_unit": 0,
        "no_exact_food": 0,
        "parse_errors": 0,
    }
    parsers: dict[object, BruteForceParser] = {}
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
            RecipeIngredientModel.original_text.is_not(None),
            sa.func.trim(RecipeIngredientModel.original_text) != "",
        )
        .order_by(RecipeIngredientModel.id)
        .limit(batch_size)
    ).all():
        pending = []
        for row, group_id in batch:
            parser = parsers.get(group_id)
            if parser is None:
                parser = BruteForceParser(group_id, session, get_locale_provider())
                parser.data_matcher = _ExactDataMatcher(parser)  # type: ignore[assignment]
                parsers[group_id] = parser
            pending.append((row, parser))

        for row, parsed in asyncio.run(_parse_batch(pending)):
            counts["examined"] += 1
            if parsed is None:
                counts["parse_errors"] += 1
                continue
            ingredient = parsed.ingredient
            if ingredient.quantity is None or ingredient.quantity <= 0:
                counts["no_positive_quantity"] += 1
                continue
            if not isinstance(ingredient.unit, IngredientUnit):
                counts["no_exact_unit"] += 1
                continue
            if not isinstance(ingredient.food, IngredientFood):
                counts["no_exact_food"] += 1
                continue

            counts["candidates"] += 1
            if apply:
                row.quantity = ingredient.quantity
                row.unit_id = ingredient.unit.id
                row.food_id = ingredient.food.id
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
