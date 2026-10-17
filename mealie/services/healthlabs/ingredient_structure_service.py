from collections.abc import Callable, Iterable
from typing import Any

from pydantic import UUID4
from sqlalchemy.orm import Session

from mealie.db.models.recipe import IngredientFoodModel, IngredientUnitModel
from mealie.lang.providers import Translator
from mealie.schema.recipe import CreateIngredientFood, CreateIngredientUnit, IngredientFood, IngredientUnit
from mealie.schema.recipe.recipe_ingredient import RecipeIngredient
from mealie.schema.response.pagination import PaginationQuery
from mealie.services.parser_services.ingredient_parser import BruteForceParser


class _ExactDataMatcher:
    def __init__(self, parser: BruteForceParser) -> None:
        query = PaginationQuery(page=1, per_page=-1)
        self.foods = self._unique_map(
            parser.repos.ingredient_foods.page_all(query).items,
            lambda item: (item.name, item.plural_name, *(alias.name for alias in item.aliases or [])),
            IngredientFoodModel.normalize,
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
            IngredientUnitModel.normalize,
        )

    @staticmethod
    def _unique_map(
        items: Iterable[Any], names: Callable[[Any], Iterable[str | None]], normalize: Callable[[str], str]
    ) -> dict[str, Any | None]:
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
        if isinstance(food, IngredientFood):
            return food
        name = food if isinstance(food, str) else food.name
        return self.foods.get(IngredientFoodModel.normalize(name))

    def find_unit_match(self, unit: IngredientUnit | CreateIngredientUnit | str) -> IngredientUnit | None:
        if isinstance(unit, IngredientUnit):
            return unit
        name = unit if isinstance(unit, str) else unit.name
        return self.units.get(IngredientUnitModel.normalize(name))


class IngredientStructureService:
    def __init__(self, session: Session, group_id: UUID4, translator: Translator) -> None:
        self.parser = BruteForceParser(group_id, session, translator)
        self.matcher = _ExactDataMatcher(self.parser)
        self.parser.data_matcher = self.matcher  # type: ignore[assignment]

    @staticmethod
    def is_candidate(ingredient: RecipeIngredient) -> bool:
        has_source = bool((ingredient.original_text or "").strip() or (ingredient.note or "").strip())
        has_structure = bool(
            ingredient.food
            or ingredient.unit
            or ingredient.referenced_recipe
            or ingredient.title
            or ingredient.substitutions
            or ingredient.quantity not in (None, 0)
        )
        return has_source and not has_structure

    def structure(self, ingredient: RecipeIngredient) -> bool:
        if not self.is_candidate(ingredient):
            return False

        original_text = (ingredient.original_text or "").strip()
        source = original_text or (ingredient.note or "").strip()
        if not source:
            return False

        try:
            parsed = self.parser.parse_one_sync(source).ingredient
        except IndexError, TypeError, ValueError:
            return False

        return self.apply_exact(ingredient, parsed)

    def apply_exact(self, ingredient: RecipeIngredient, parsed: RecipeIngredient) -> bool:
        if not self.is_candidate(ingredient):
            return False

        original_text = (ingredient.original_text or "").strip()
        source = original_text or (ingredient.note or "").strip()
        parsed_food = parsed.food
        parsed_unit = parsed.unit

        if parsed.quantity is None or parsed.quantity <= 0 or parsed_food is None or parsed_unit is None:
            return False

        food = self.matcher.foods.get(IngredientFoodModel.normalize(parsed_food.name))
        unit = self.matcher.units.get(IngredientUnitModel.normalize(parsed_unit.name))
        if not isinstance(food, IngredientFood) or not isinstance(unit, IngredientUnit):
            return False

        ingredient.quantity = parsed.quantity
        ingredient.unit = unit
        ingredient.food = food
        ingredient.original_text = source
        if not original_text or not ingredient.note:
            ingredient.note = parsed.note.strip() if parsed.note else ""
        return True
