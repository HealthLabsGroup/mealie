import json
import runpy
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
)
from mealie.db.models.recipe.ingredient import IngredientFoodAliasModel, IngredientFoodModel
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.recipe import SaveIngredientFood
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
    session.add(reviewed_match)
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
    _, source_foods = _source(session, ["Coverage source"], provider="cofid")
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
            match_method="manual",
            confidence=1,
            reviewed_by_user_id=provisioned["userId"],
            reviewed_at=get_utc_now(),
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
    assert report["source_versions"][0]["country"] == "GB"
    assert report["mock_source_releases"] == 0
    serialized = json.dumps(report)
    assert private_recipe_name not in serialized
    assert private_original_text not in serialized
    assert subject not in serialized
