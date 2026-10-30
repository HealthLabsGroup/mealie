from copy import deepcopy
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsNutrientFood,
    HealthLabsRecipeMetadataEstimate,
)
from mealie.db.models.recipe import IngredientFoodModel
from mealie.db.models.recipe.recipe import HealthLabsRecipeRevision
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.healthlabs.nutrition import HealthLabsFoodMatchWrite
from mealie.schema.recipe.recipe_ingredient import CreateIngredientFood
from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler
from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService


@pytest.fixture(params=[("Rice plant", "Rice"), ("r" * 81, "Rice"), ("rice", "R" * 121), ("rice", "Rice")])
def accepted_plant_recipe(
    request,
    api_client,
    healthlabs_provision,
    healthlabs_set_capabilities,
    healthlabs_nutrition_source,
    unfiltered_database,
):
    plant_id, food_name = request.param
    # These are supported public write inputs, independent of inference constraints.
    CreateIngredientFood(name=food_name)
    identity, headers = healthlabs_provision()
    healthlabs_set_capabilities(
        identity["userId"],
        HealthLabsCapability.RECIPES_READ_OWN,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.NUTRITION_WRITE,
    )
    created = api_client.post(
        "/api/healthlabs/v1/recipes",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "name": f"Recorded plants {uuid4()}",
            "servings": 1,
            "totalTimeSeconds": 0,
            "prepTimeSeconds": 0,
            "performTimeSeconds": 0,
            "authoring": {"ephraEffort1": True},
            "ingredients": [{"quantity": 100, "food": {"name": food_name}, "unit": {"name": "gram"}}],
        },
    )
    assert created.status_code == 201, created.text
    recipe = created.json()
    food_id = recipe["ingredients"][0]["food"]["id"]
    _, source_food_id = healthlabs_nutrition_source(name="Recorded source " + "s" * 121)
    session = unfiltered_database.session
    source_food = session.get(HealthLabsNutrientFood, UUID(source_food_id))
    source_food.nutrient_values = {
        **source_food.nutrient_values,
        "fibre_g": {"amount": 0, "unit": "g"},
        "water_ml": {"amount": None, "unit": "ml"},
    }
    session.commit()
    body = {"nutrientFoodId": source_food_id, "country": "GB", "plantId": plant_id}
    assert HealthLabsFoodMatchWrite.model_validate(body).plant_id == plant_id
    matched = api_client.put(
        f"/api/healthlabs/v1/nutrition/foods/{food_id}/match",
        headers={**headers, "Idempotency-Key": str(uuid4()), "X-HealthLabs-Access-Reason": "Record plant identity"},
        json=body,
    )
    assert matched.status_code == 200, matched.text
    assert matched.json()["nutrition"]["plantId"] == plant_id
    repos = AllRepositories(session, group_id=UUID(identity["groupId"]), household_id=None)
    native_recipe = repos.recipes.get_one(UUID(recipe["id"]), "id")
    revision = session.get(HealthLabsRecipeRevision, UUID(recipe["revisionId"]))
    original = deepcopy(revision.snapshot)
    service = RecipeMetadataService(session)
    fingerprint = service.inputs(native_recipe).input_fingerprint
    yield recipe, headers, repos, native_recipe, plant_id, food_name
    assert service.inputs(native_recipe).input_fingerprint == fingerprint
    session.refresh(revision)
    assert revision.snapshot == original
    assert session.get(IngredientFoodModel, UUID(food_id)).name == food_name
    match = session.scalar(
        sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == UUID(food_id))
    )
    assert match.plant_id == plant_id


def test_public_recipe_reads_preserve_supported_recorded_plant_values(api_client, accepted_plant_recipe):
    recipe, headers, _, _, plant_id, food_name = accepted_plant_recipe
    current = api_client.get(f"/api/healthlabs/v1/recipes/{recipe['id']}", headers=headers)
    listed = api_client.get("/api/healthlabs/v1/recipes", params={"q": recipe["name"]}, headers=headers)
    revision = api_client.get(
        f"/api/healthlabs/v1/recipes/{recipe['id']}/revisions/{recipe['revisionId']}", headers=headers
    )
    assert current.status_code == listed.status_code == revision.status_code == 200
    for body in (current.json(), listed.json()["items"][0], revision.json()["recipe"]):
        assert body["metadata"]["plants"]["recorded"] == [{"plantId": plant_id, "name": food_name, "quantities": []}]
        assert body["metadata"]["plants"]["estimated"] == []
        assert body["metadata"]["plants"]["status"] == "complete"
        assert body["nutritionMetadata"]["complete"] is True
        assert body["nutritionMetadata"]["perServing"]["fibreG"]["verified"] == 0
        assert body["nutritionMetadata"]["perServing"]["waterMl"]["value"] is None


def test_source_complete_scanner_handles_recorded_plants_without_enqueuing_inference(accepted_plant_recipe):
    recipe, _, repos, native_recipe, plant_id, food_name = accepted_plant_recipe
    service = RecipeMetadataService(repos.session)
    assert service.inputs(native_recipe).missing_fields() == []
    jobs = HealthLabsRecipeMetadataEstimate
    existing_ids = set(repos.session.scalars(sa.select(jobs.id).where(jobs.recipe_id == native_recipe.id)))
    report = RecipeMetadataScheduler(repos.session).scan_page(group_ids={native_recipe.group_id}, page_size=1)
    assert report.recipe_ids == (UUID(recipe["id"]),)
    assert report.scanned == 1
    assert service.project(native_recipe)[1].plants.recorded[0].model_dump()["plant_id"] == plant_id
    assert service.project(native_recipe)[1].plants.recorded[0].name == food_name
    assert set(repos.session.scalars(sa.select(jobs.id).where(jobs.recipe_id == native_recipe.id))) == existing_ids
    assert (
        repos.session.scalar(
            sa.select(sa.func.count())
            .select_from(jobs)
            .where(jobs.recipe_id == native_recipe.id, jobs.state == "pending")
        )
        == 0
    )
