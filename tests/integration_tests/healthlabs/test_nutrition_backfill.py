import json
import runpy
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.db.models.recipe import RecipeIngredientModel, RecipeIngredientSubstitutionModel
from mealie.db.models.recipe.ingredient import IngredientFoodAliasModel, IngredientFoodModel
from mealie.db.models.users import User
from mealie.repos.all_repositories import get_repositories
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.recipe import SaveIngredientFood, SaveIngredientUnit
from tests.utils import TestUser

ROOT = Path(__file__).parents[3]


def _script(name: str) -> dict:
    return runpy.run_path(str(ROOT / "scripts" / "healthlabs" / name))


def _source(
    session,
    names: list[str],
    *,
    provider: str | None = None,
) -> tuple[HealthLabsNutrientSourceRelease, list[HealthLabsNutrientFood]]:
    release = HealthLabsNutrientSourceRelease(
        provider=provider or f"test-{uuid4()}",
        version="2026-10",
        country="GB",
        source_url="https://example.test/source",
        license_url="https://example.test/licence",
        attribution="Test source",
        sha256=uuid4().hex.ljust(64, "0"),
    )
    session.add(release)
    session.flush()
    foods = [
        HealthLabsNutrientFood(
            source_release_id=release.id,
            source_food_id=f"source-{index}",
            name=name,
            nutrient_values={
                "kcal": {"amount": 10, "unit": "kcal"},
                "protein_g": {"amount": 1, "unit": "g"},
                "fibre_g": {"amount": 1, "unit": "g"},
                "carbs_g": {"amount": 1, "unit": "g"},
                "fat_g": {"amount": 1, "unit": "g"},
                "water_ml": {"amount": 1, "unit": "ml"},
            },
        )
        for index, name in enumerate(names)
    ]
    session.add_all(foods)
    session.flush()
    return release, foods


def test_backfill_is_idempotent_and_classifies_every_food(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    token = uuid4().hex
    names = [
        f"Apple {token}",
        f"Courgette {token}",
        f"Rice {token}",
        f"Rice {token}",
        f"Reviewed banana {token}",
    ]
    _, source_foods = _source(session, names)
    exact = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=names[0])
    )
    alias = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=f"Green squash {token}")
    )
    ambiguous = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=names[2])
    )
    unknown = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=f"Unknown food {token}")
    )
    reviewed = unique_user_fn_scoped.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=unique_user_fn_scoped.group_id, name=f"Banana {token}")
    )
    session.add(IngredientFoodAliasModel(session=session, food_id=alias.id, name=names[1]))
    reviewed_match = HealthLabsFoodMatch(
        ingredient_food_id=reviewed.id,
        nutrient_food_id=source_foods[-1].id,
        country="GB",
        match_method="manual",
        confidence=1,
        reviewed_by_user_id=unique_user_fn_scoped.user_id,
        reviewed_at=get_utc_now(),
    )
    auto_match = HealthLabsFoodMatch(
        ingredient_food_id=exact.id,
        nutrient_food_id=source_foods[0].id,
        country="GB",
        match_method="exact",
        confidence=1,
        auto_applied=True,
    )
    session.add_all([reviewed_match, auto_match])
    session.commit()
    reviewed_snapshot = (reviewed_match.id, reviewed_match.nutrient_food_id, reviewed_match.reviewed_at)
    backfill = _script("backfill-food-matches.py")["backfill_food_matches"]
    matches_before = session.scalar(sa.select(sa.func.count()).select_from(HealthLabsFoodMatch)) or 0

    first = backfill(session, country="GB", batch_size=2)
    matches_after_first = session.scalar(sa.select(sa.func.count()).select_from(HealthLabsFoodMatch)) or 0
    second = backfill(session, country="GB", batch_size=2)

    assert first["created"] == matches_after_first - matches_before
    assert first["total"] == session.scalar(sa.select(sa.func.count()).select_from(IngredientFoodModel))
    assert second == {**first, "created": 0}
    assert first["total"] == first["matched"] + first["review_required"] + first["unknown"]
    effective_matches = session.scalar(
        sa.select(sa.func.count())
        .select_from(HealthLabsFoodMatch)
        .where(
            sa.or_(
                HealthLabsFoodMatch.auto_applied.is_(True),
                sa.and_(
                    HealthLabsFoodMatch.reviewed_at.is_not(None),
                    HealthLabsFoodMatch.reviewed_by_user_id.is_not(None),
                ),
            )
        )
    )
    assert first["matched"] == effective_matches
    persisted = session.scalars(
        sa.select(HealthLabsFoodMatch).where(
            HealthLabsFoodMatch.ingredient_food_id.in_([exact.id, alias.id, ambiguous.id, unknown.id, reviewed.id])
        )
    ).all()
    assert {row.ingredient_food_id for row in persisted} == {exact.id, alias.id, reviewed.id}
    session.refresh(reviewed_match)
    assert (reviewed_match.id, reviewed_match.nutrient_food_id, reviewed_match.reviewed_at) == reviewed_snapshot


def test_coverage_report_contains_only_aggregate_workspace_data(
    api_client: TestClient,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    subject = f"subject-{uuid4()}"
    provisioned, headers = healthlabs_provision(subject)
    private_recipe_name = f"Private recipe {uuid4()}"
    private_original_text = f"secret ingredient text {uuid4()}"
    response = api_client.post(
        "/api/healthlabs/v1/recipes",
        headers={**headers, "Idempotency-Key": f"coverage-{uuid4()}"},
        json={
            "name": private_recipe_name,
            "servings": 1,
            "ingredients": [
                {"quantity": 2, "food": {"name": "Coverage food"}, "unit": {"name": "scoop"}},
                {"originalText": private_original_text},
            ],
        },
    )
    assert response.status_code == 201, response.text
    session = unfiltered_database.session
    source_release, source_foods = _source(session, ["Coverage source"], provider="cofid")
    source_foods[0].nutrient_values = {
        **source_foods[0].nutrient_values,
        "water_ml": {"amount": None, "unit": "ml"},
    }
    food_id = response.json()["ingredients"][0]["food"]["id"]
    session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=food_id,
            nutrient_food_id=source_foods[0].id,
            country="GB",
            match_method="exact",
            confidence=1,
            auto_applied=True,
        )
    )
    session.commit()
    report = _script("report-nutrition-coverage.py")["nutrition_coverage_report"](
        session,
        external_subject_id=subject,
    )

    assert report["recipes"] == 1
    assert report["foods"] == {"total": 1, "matched": 1, "review_required": 0, "unknown": 0}
    assert report["ingredients"] == {
        "total": 2,
        "structured": 1,
        "unparsed": 1,
        "matched": 1,
        "review_required": 0,
        "unknown": 0,
        "missing_conversion": 1,
        "missing_core_nutrient": 1,
    }
    assert report["ingredients"]["total"] == (report["ingredients"]["structured"] + report["ingredients"]["unparsed"])
    assert report["ingredients"]["structured"] == sum(
        report["ingredients"][status] for status in ("matched", "review_required", "unknown")
    )
    assert any(
        source["provider"] == source_release.provider
        and source["version"] == source_release.version
        and source["country"] == source_release.country
        and source["food_matches"] == 1
        for source in report["source_versions"]
    )
    assert report["mock_source_releases"] == 0
    serialized = json.dumps(report)
    assert private_recipe_name not in serialized
    assert private_original_text not in serialized
    assert subject not in serialized


def test_structure_backfill_requires_exact_complete_parse(
    api_client: TestClient,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    identity, headers = healthlabs_provision()
    session = unfiltered_database.session
    user = session.get(User, identity["userId"])
    assert user is not None
    repos = get_repositories(session, group_id=user.group_id)
    token = uuid4().hex
    unit_name = f"scoop{token}"
    food_name = f"Exact apple {token}"
    ambiguous_name = f"ambiguous{token}"
    unit = repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=unit_name))
    food = repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=food_name))
    ambiguous_foods = [
        repos.ingredient_foods.create(
            SaveIngredientFood(group_id=user.group_id, name=f"Ambiguous food {index} {token}")
        )
        for index in range(2)
    ]
    session.add_all(
        [IngredientFoodAliasModel(session=session, food_id=row.id, name=ambiguous_name) for row in ambiguous_foods]
    )
    session.commit()
    original_texts = [
        f"2 {unit_name} {food_name}",
        f"1 {unit_name} {ambiguous_name}",
        f"1 {unit_name} Exact aple {token}",
        food_name,
        f"1 scooop{token} {food_name}",
        f"3 {unit_name} {food_name}",
    ]
    created = api_client.post(
        "/api/healthlabs/v1/recipes",
        headers={**headers, "Idempotency-Key": f"structure-{uuid4()}"},
        json={
            "name": f"Structure backfill {token}",
            "servings": 1,
            "ingredients": [{"originalText": value} for value in original_texts],
        },
    )
    assert created.status_code == 201, created.text
    recipe_id = UUID(created.json()["id"])
    legacy_rows = list(
        session.scalars(
            sa.select(RecipeIngredientModel)
            .where(RecipeIngredientModel.recipe_id == recipe_id)
            .order_by(RecipeIngredientModel.position)
        )
    )
    legacy_rows[0].unit_id = None
    legacy_rows[0].food_id = None
    legacy_rows[-1].quantity = 0
    legacy_rows[-1].unit_id = None
    legacy_rows[-1].food_id = None
    session.add(RecipeIngredientSubstitutionModel(ingredient_id=legacy_rows[-1].id, note="preserve"))
    session.commit()
    backfill = _script("backfill-ingredient-structure.py")["backfill_ingredient_structure"]

    dry_run = backfill(session, apply=False, batch_size=2)
    rows = list(
        session.scalars(
            sa.select(RecipeIngredientModel)
            .where(RecipeIngredientModel.recipe_id == recipe_id)
            .order_by(RecipeIngredientModel.position)
        )
    )

    assert dry_run["candidates"] == 1
    assert dry_run["updated"] == 0
    assert all(row.food_id is None and row.unit_id is None for row in rows)

    applied = backfill(session, apply=True, batch_size=2)
    session.expire_all()
    rows = list(
        session.scalars(
            sa.select(RecipeIngredientModel)
            .where(RecipeIngredientModel.recipe_id == recipe_id)
            .order_by(RecipeIngredientModel.position)
        )
    )

    assert applied["candidates"] == 1
    assert applied["updated"] == 1
    assert (rows[0].quantity, rows[0].unit_id, rows[0].food_id, rows[0].original_text) == (
        2,
        unit.id,
        food.id,
        original_texts[0],
    )
    assert all(row.food_id is None and row.unit_id is None for row in rows[1:])
    assert [row.original_text for row in rows] == original_texts

    repeated = backfill(session, apply=True, batch_size=2)
    assert repeated["updated"] == 0


def test_runtime_recipe_writes_structure_exact_ingredients_and_calculate_portions(
    api_client: TestClient,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    identity, headers = healthlabs_provision()
    session = unfiltered_database.session
    user = session.get(User, identity["userId"])
    assert user is not None
    repos = get_repositories(session, group_id=user.group_id)
    token = uuid4().hex
    unit_name = f"measure{token}"
    food_name = f"Portioned oats {token}"
    ambiguous_name = f"ambiguous-runtime-{token}"
    repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=unit_name))
    food = repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=food_name))
    ambiguous_foods = [
        repos.ingredient_foods.create(
            SaveIngredientFood(group_id=user.group_id, name=f"Runtime ambiguous food {index} {token}")
        )
        for index in range(2)
    ]
    session.add_all(
        [IngredientFoodAliasModel(session=session, food_id=row.id, name=ambiguous_name) for row in ambiguous_foods]
    )
    _, source_foods = _source(session, [food_name])
    nutrient_food = source_foods[0]
    session.add_all(
        [
            HealthLabsFoodMatch(
                ingredient_food_id=food.id,
                nutrient_food_id=nutrient_food.id,
                country="GB",
                match_method="manual",
                confidence=1,
                reviewed_by_user_id=user.id,
                reviewed_at=datetime.now(UTC),
            ),
            HealthLabsFoodPortion(
                nutrient_food_id=nutrient_food.id,
                description=unit_name,
                amount=1,
                unit=unit_name,
                grams=50,
            ),
        ]
    )
    session.commit()
    created = api_client.post(
        "/api/healthlabs/v1/recipes",
        headers={**headers, "Idempotency-Key": f"portion-structure-{uuid4()}"},
        json={
            "name": f"Portion structure {token}",
            "servings": 1,
            "ingredients": [
                {"originalText": f"2 {unit_name} {food_name}"},
                {"originalText": f"1 {unit_name} Portioned oat {token}"},
                {"originalText": f"1 {unit_name} {ambiguous_name}"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    ingredient = body["ingredients"][0]
    assert ingredient["food"] is not None
    assert ingredient["unit"] is not None
    assert ingredient["nutrition"]["resolvedGrams"] == 100
    assert ingredient["nutrition"]["nutrients"]["kcal"] == 10
    assert body["calculatedNutrition"]["totals"]["kcal"] is None
    assert body["ingredients"][1]["food"] is None
    assert body["ingredients"][2]["food"] is None

    source_text = f"3 {unit_name} {food_name}, toasted"
    updated = api_client.put(
        f"/api/healthlabs/v1/recipes/{body['id']}",
        headers={**headers, "Idempotency-Key": f"runtime-update-{uuid4()}"},
        json={
            "name": body["name"],
            "servings": 1,
            "ingredients": [{"note": source_text}],
        },
    )

    assert updated.status_code == 200, updated.text
    updated_ingredient = updated.json()["ingredients"][0]
    assert updated_ingredient["quantity"] == 3
    assert updated_ingredient["food"]["id"] == str(food.id)
    assert updated_ingredient["unit"] is not None
    assert updated_ingredient["note"] == "toasted"
    assert updated_ingredient["originalText"] == source_text
    assert updated_ingredient["nutrition"]["resolvedGrams"] == 150
    assert updated.json()["calculatedNutrition"]["totals"]["kcal"] == 15
