from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsFoodPortionMapping,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionCuratorDecision,
    HealthLabsNutritionWork,
)
from mealie.schema.healthlabs.nutrition import HealthLabsNutritionStatus
from mealie.schema.recipe import Recipe, RecipeIngredient, SaveIngredientFood, SaveIngredientUnit
from mealie.services.healthlabs.nutrition_service import NutritionService
from tests.utils import TestUser

CORE_VALUES = {
    "kcal": {"amount": 123.4567, "unit": "kcal"},
    "protein_g": {"amount": 10, "unit": "g"},
    "fibre_g": {"amount": 0, "unit": "g"},
    "carbs_g": {"amount": 20, "unit": "g"},
    "fat_g": {"amount": 5, "unit": "g"},
    "water_ml": {"amount": None, "unit": "ml"},
}


def _source(user: TestUser, *, country: str = "GB", provider: str | None = None):
    source = HealthLabsNutrientSourceRelease(
        provider=provider or f"source-{uuid4()}",
        version="2026-10",
        country=country,
        source_url="https://example.test/source",
        license_url="https://example.test/licence",
        attribution="Test source",
        sha256=uuid4().hex.ljust(64, "0"),
    )
    user.repos.session.add(source)
    user.repos.session.flush()
    return source


def _nutrient_food(
    user: TestUser,
    source: HealthLabsNutrientSourceRelease,
    *,
    name: str,
    state: str | None = None,
    values: dict | None = None,
):
    food = HealthLabsNutrientFood(
        source_release_id=source.id,
        source_food_id=str(uuid4()),
        name=name,
        preparation_state=state,
        nutrient_values=values or CORE_VALUES,
    )
    user.repos.session.add(food)
    user.repos.session.commit()
    return food


def _ingredient_food(user: TestUser, name: str, *, aliases: list[str] | None = None):
    return user.repos.ingredient_foods.create(
        SaveIngredientFood(
            group_id=user.group_id,
            name=name,
            aliases=[{"name": alias} for alias in aliases or []],
        )
    )


def _reviewed_match(user: TestUser, food_id, nutrient_food_id, *, country: str = "GB") -> None:
    user.repos.session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=food_id,
            nutrient_food_id=nutrient_food_id,
            country=country,
            match_method="manual",
            confidence=1,
            reviewed_by_user_id=user.user_id,
            reviewed_at=datetime.now(UTC),
        )
    )
    user.repos.session.commit()


def test_match_food_returns_reviewed_match_before_automatic_candidates(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    food = _ingredient_food(user, f"unrelated-{uuid4()}")
    source_food = _nutrient_food(user, _source(user), name="Reviewed source food")
    _reviewed_match(user, food.id, source_food.id)

    match = NutritionService(user.repos.session).match_food(food.id, "FR")

    assert match is not None
    assert match.status == HealthLabsNutritionStatus.MATCHED
    assert match.reviewed is True
    assert match.nutrient_food_id == source_food.id


def test_match_food_finds_normalized_name_and_curated_alias_candidates(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    source = _source(user)
    source_food = _nutrient_food(user, source, name="Crème-fraîche")
    exact = _ingredient_food(user, "Creme fraiche")
    alias = _ingredient_food(user, f"cultured cream-{uuid4()}", aliases=["Creme fraiche"])
    service = NutritionService(user.repos.session)

    exact_match = service.match_food(exact.id, "GB")
    alias_match = service.match_food(alias.id, "GB")

    assert exact_match is not None
    assert exact_match.status == HealthLabsNutritionStatus.REVIEW_REQUIRED
    assert exact_match.nutrient_food_id == source_food.id
    assert exact_match.match_method == "exact"
    assert alias_match is not None
    assert alias_match.nutrient_food_id == source_food.id
    assert alias_match.match_method == "alias"


def test_match_food_rejects_ambiguity_preparation_conflict_and_country_gap(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    source = _source(user)
    _nutrient_food(user, source, name="Ambiguous food")
    _nutrient_food(user, source, name="Ambiguous food")
    ambiguous = _ingredient_food(user, "Ambiguous food")

    cooked = _nutrient_food(user, source, name="lentils", state="cooked")
    raw = _ingredient_food(user, f"lentils raw-{uuid4()}", aliases=[cooked.name])
    gb_only = _nutrient_food(user, source, name=f"licensed only in gb-{uuid4()}")
    country_gap = _ingredient_food(user, gb_only.name)
    service = NutritionService(user.repos.session)

    assert service.match_food(ambiguous.id, "GB") is None
    assert service.match_food(raw.id, "GB") is None
    assert service.match_food(country_gap.id, "PL") is None


@pytest.mark.parametrize(
    ("name", "standard_quantity", "standard_unit", "quantity", "expected_grams"),
    [
        ("gram", 1, "gram", 250, 250),
        ("kilogram", 1, "kilogram", 0.25, 250),
        ("ounce", 1, "ounce", 2, 56.699),
    ],
)
def test_food_facts_converts_mass_units(
    unique_user_fn_scoped: TestUser,
    name: str,
    standard_quantity: float,
    standard_unit: str,
    quantity: float,
    expected_grams: float,
) -> None:
    user = unique_user_fn_scoped
    food = _ingredient_food(user, f"mass-food-{uuid4()}")
    nutrient_food = _nutrient_food(user, _source(user), name=food.name)
    _reviewed_match(user, food.id, nutrient_food.id)
    unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(
            group_id=user.group_id,
            name=f"{name}-{uuid4()}",
            standard_quantity=standard_quantity,
            standard_unit=standard_unit,
        )
    )

    facts = NutritionService(user.repos.session).food_facts(food.id, unit.id, quantity)

    assert facts.complete is False
    assert facts.resolved_grams == expected_grams
    assert facts.nutrients.kcal == pytest.approx(round(123.4567 * expected_grams / 100, 3))
    assert facts.nutrients.fibre_g == 0
    assert facts.nutrients.water_ml is None


@pytest.mark.parametrize(
    ("unit_name", "standard_unit", "portion_amount", "portion_unit", "quantity", "expected_grams"),
    [
        ("millilitre", "milliliter", 100, "ml", 50, 51.5),
        ("piece", None, 1, "piece", 2, 140),
    ],
)
def test_food_facts_uses_source_backed_volume_and_piece_portions(
    unique_user_fn_scoped: TestUser,
    unit_name: str,
    standard_unit: str | None,
    portion_amount: float,
    portion_unit: str,
    quantity: float,
    expected_grams: float,
) -> None:
    user = unique_user_fn_scoped
    food = _ingredient_food(user, f"portion-food-{uuid4()}")
    nutrient_food = _nutrient_food(user, _source(user), name=food.name)
    _reviewed_match(user, food.id, nutrient_food.id)
    user.repos.session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=nutrient_food.id,
            description=portion_unit,
            amount=portion_amount,
            unit=portion_unit,
            grams=103 if portion_unit == "ml" else 70,
        )
    )
    unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(
            group_id=user.group_id,
            name=f"{unit_name}-{uuid4()}",
            aliases=[{"name": unit_name}],
            standard_quantity=1 if standard_unit else None,
            standard_unit=standard_unit,
        )
    )

    facts = NutritionService(user.repos.session).food_facts(food.id, unit.id, quantity)

    assert facts.resolved_grams == expected_grams


def test_food_facts_prefers_exact_curator_portion_mapping(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    food = _ingredient_food(user, f"mapped-food-{uuid4()}")
    nutrient_food = _nutrient_food(user, _source(user), name=food.name)
    _reviewed_match(user, food.id, nutrient_food.id)
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"scoop-{uuid4()}"))
    portion = HealthLabsFoodPortion(
        nutrient_food_id=nutrient_food.id,
        description="one piece",
        amount=2,
        unit="piece",
        grams=90,
    )
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind="portion_resolution",
        target_id="1",
        input_sha256="a" * 64,
        state="completed",
        next_attempt_at=datetime.now(UTC),
    )
    user.repos.session.add_all([portion, work])
    user.repos.session.flush()
    decision = HealthLabsNutritionCuratorDecision(
        work_id=work.id,
        group_id=user.group_id,
        kind=work.kind,
        target_id=work.target_id,
        work_input_sha256=work.input_sha256,
        curator_sha256="b" * 64,
        agent_user_id=user.user_id,
        state="approved",
        next_attempt_at=datetime.now(UTC),
        candidate_snapshot={"portions": [{"id": str(portion.id), "label": "one piece"}]},
        provider_name="test",
        provider_model="test",
        prompt_version="healthlabs-nutrition-curator-v1",
    )
    user.repos.session.add(decision)
    user.repos.session.flush()
    user.repos.session.add(
        HealthLabsFoodPortionMapping(
            group_id=user.group_id,
            ingredient_food_id=food.id,
            ingredient_unit_id=unit.id,
            food_portion_id=portion.id,
            reviewed_by_user_id=user.user_id,
            curator_decision_id=decision.id,
        )
    )
    user.repos.session.commit()

    facts = NutritionService(user.repos.session).food_facts(food.id, unit.id, 4)

    assert facts.resolved_grams == 180


def test_food_facts_keeps_missing_conversion_unknown(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    food = _ingredient_food(user, f"unknown-unit-food-{uuid4()}")
    nutrient_food = _nutrient_food(user, _source(user), name=food.name)
    _reviewed_match(user, food.id, nutrient_food.id)
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"handful-{uuid4()}"))

    facts = NutritionService(user.repos.session).food_facts(food.id, unit.id, 1)

    assert facts.resolved_grams is None
    assert facts.missing_reason == "conversion"
    assert all(value is None for value in facts.nutrients.model_dump().values())


def test_unreviewed_match_never_contributes_recipe_totals(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    food = _ingredient_food(user, f"unreviewed-food-{uuid4()}")
    nutrient_food = _nutrient_food(user, _source(user), name=food.name)
    user.repos.session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=food.id,
            nutrient_food_id=nutrient_food.id,
            country="GB",
            match_method="exact",
            confidence=1,
        )
    )
    user.repos.session.commit()
    unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=user.group_id, name=f"gram-{uuid4()}", standard_quantity=1, standard_unit="gram")
    )
    recipe = Recipe(
        name="Awaiting review",
        recipe_servings=1,
        recipe_ingredient=[RecipeIngredient(reference_id=uuid4(), quantity=100, food=food, unit=unit)],
    )

    nutrition = NutritionService(user.repos.session).calculate_recipe(recipe, "GB")

    assert nutrition.complete is False
    assert all(value is None for value in nutrition.totals.model_dump().values())


def test_calculate_recipe_rounds_totals_and_keeps_per_serving_null_for_zero_servings(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    food = _ingredient_food(user, f"recipe-food-{uuid4()}")
    source = _source(user)
    nutrient_food = _nutrient_food(user, source, name=food.name)
    _reviewed_match(user, food.id, nutrient_food.id)
    unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=user.group_id, name=f"gram-{uuid4()}", standard_quantity=1, standard_unit="gram")
    )
    reference_id = uuid4()
    recipe = Recipe(
        name="Nutrition calculation",
        recipe_servings=0,
        recipe_ingredient=[RecipeIngredient(reference_id=reference_id, quantity=33.333, food=food, unit=unit)],
    )

    nutrition = NutritionService(user.repos.session).calculate_recipe(recipe, "GB")

    assert nutrition.complete is False
    assert nutrition.totals.kcal == 41.152
    assert nutrition.totals.fibre_g == 0
    assert nutrition.totals.water_ml is None
    assert all(value is None for value in nutrition.per_serving.model_dump().values())
    assert nutrition.missing_ingredient_references == [reference_id]
    assert [(item.provider, item.version) for item in nutrition.source_versions] == [(source.provider, source.version)]


def test_calculate_recipe_rounds_only_after_summing_ingredients(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    food = _ingredient_food(user, f"precise-recipe-food-{uuid4()}")
    values = {**CORE_VALUES, "water_ml": {"amount": 1, "unit": "ml"}}
    nutrient_food = _nutrient_food(user, _source(user), name=food.name, values=values)
    _reviewed_match(user, food.id, nutrient_food.id)
    unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=user.group_id, name=f"gram-{uuid4()}", standard_quantity=1, standard_unit="gram")
    )
    recipe = Recipe(
        name="Round once",
        recipe_servings=1,
        recipe_ingredient=[
            RecipeIngredient(reference_id=uuid4(), quantity=0.003, food=food, unit=unit),
            RecipeIngredient(reference_id=uuid4(), quantity=0.003, food=food, unit=unit),
        ],
    )

    nutrition = NutritionService(user.repos.session).calculate_recipe(recipe, "GB")

    assert nutrition.complete is True
    assert nutrition.totals.kcal == 0.007
