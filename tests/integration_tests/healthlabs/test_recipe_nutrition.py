from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from mealie.db.models.healthlabs import HealthLabsFoodMatch, HealthLabsNutrientFood, HealthLabsNutrientSourceRelease
from mealie.repos.repository_factory import AllRepositories


def test_current_and_revision_recipe_reads_expose_the_same_calculated_nutrition(
    api_client: TestClient,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    identity, headers = healthlabs_provision()
    reference_id = str(uuid4())
    created = api_client.post(
        "/api/healthlabs/v1/recipes",
        headers={**headers, "Idempotency-Key": f"nutrition-{uuid4()}"},
        json={
            "name": f"Nutrition recipe {uuid4()}",
            "servings": 2,
            "ingredients": [
                {
                    "referenceId": reference_id,
                    "quantity": 100,
                    "food": {"name": f"Nutrition food {uuid4()}"},
                    "unit": {"name": "gram", "abbreviation": "g"},
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    recipe = created.json()
    ingredient_food_id = UUID(recipe["ingredients"][0]["food"]["id"])

    session = unfiltered_database.session
    source = HealthLabsNutrientSourceRelease(
        provider=f"api-source-{uuid4()}",
        version="2026-10",
        country="GB",
        source_url="https://example.test/source",
        license_url="https://example.test/licence",
        attribution="Test source",
        sha256=uuid4().hex.ljust(64, "0"),
    )
    session.add(source)
    session.flush()
    nutrient_food = HealthLabsNutrientFood(
        source_release_id=source.id,
        source_food_id=str(uuid4()),
        name="Nutrition food",
        nutrient_values={
            "kcal": {"amount": 50, "unit": "kcal"},
            "protein_g": {"amount": 10, "unit": "g"},
            "fibre_g": {"amount": 2, "unit": "g"},
            "carbs_g": {"amount": 5, "unit": "g"},
            "fat_g": {"amount": 1, "unit": "g"},
            "water_ml": {"amount": 80, "unit": "ml"},
        },
    )
    session.add(nutrient_food)
    session.flush()
    session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=ingredient_food_id,
            nutrient_food_id=nutrient_food.id,
            country="GB",
            match_method="manual",
            confidence=1,
            reviewed_by_user_id=UUID(identity["userId"]),
            reviewed_at=datetime.now(UTC),
        )
    )
    session.commit()

    current = api_client.get(f"/api/healthlabs/v1/recipes/{recipe['id']}", headers=headers)
    revision = api_client.get(
        f"/api/healthlabs/v1/recipes/{recipe['id']}/revisions/{recipe['revisionId']}",
        headers=headers,
    )

    assert current.status_code == 200, current.text
    assert revision.status_code == 200, revision.text
    current_body = current.json()
    revision_body = revision.json()["recipe"]
    assert current_body["ingredients"][0]["food"]["nutrition"]["status"] == "matched"
    assert current_body["ingredients"][0]["nutrition"]["resolvedGrams"] == 100
    assert current_body["calculatedNutrition"]["totals"]["kcal"] == 50
    assert current_body["calculatedNutrition"]["perServing"]["kcal"] == 25
    assert current_body["calculatedNutrition"] == revision_body["calculatedNutrition"]
    assert current_body["nutritionMetadata"]["perServing"]["proteinG"]["verified"] == 5
    assert current_body["nutritionMetadata"]["nativeComplete"] is True
    assert current_body["metadata"]["servings"]["recorded"] == 2
    assert current_body["metadata"]["source"]["attributionStatus"] == "not_recorded"
