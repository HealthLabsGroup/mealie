import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from mealie.core.config import get_app_settings
from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutritionCuratorDecision,
    HealthLabsNutritionProviderUsage,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import RecipeIngredientModel
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.services.healthlabs.nutrition_curator_identity_service import NutritionCuratorIdentityService
from mealie.services.healthlabs.nutrition_worker import NutritionReconciliationWorker


def test_managed_user_can_parse_ingredients_through_healthlabs_api(
    api_client: TestClient,
    healthlabs_provision: Callable,
) -> None:
    _, headers = healthlabs_provision()

    response = api_client.post(
        "/api/healthlabs/v1/nutrition/ingredients/parse",
        json={"parser": "nlp", "ingredients": ["3 chicken eggs"]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    ingredient = response.json()[0]["ingredient"]
    assert ingredient["quantity"] == 3
    assert ingredient["originalText"] == "3 chicken eggs"


def test_managed_ingredient_parser_rejects_openai(
    api_client: TestClient,
    healthlabs_provision: Callable,
    monkeypatch: MonkeyPatch,
) -> None:
    _, headers = healthlabs_provision()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("provider-backed parser must not be resolved")

    monkeypatch.setattr("mealie.routes.healthlabs.nutrition.get_parser", fail_if_called)
    response = api_client.post(
        "/api/healthlabs/v1/nutrition/ingredients/parse",
        json={"parser": "openai", "ingredients": ["3 chicken eggs"]},
        headers=headers,
    )

    assert response.status_code == 422


def test_managed_ingredient_parser_rejects_oversized_inputs(
    api_client: TestClient,
    healthlabs_provision: Callable,
) -> None:
    _, headers = healthlabs_provision()

    invalid_ingredients = ([], ["egg"] * 51, ["e" * 2001], ["e" * 1000] * 11)
    for ingredients in invalid_ingredients:
        response = api_client.post(
            "/api/healthlabs/v1/nutrition/ingredients/parse",
            json={"parser": "nlp", "ingredients": ingredients},
            headers=headers,
        )
        assert response.status_code == 422


def test_food_search_detail_match_replace_portion_and_clear(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
    healthlabs_nutrition_source: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    owner, owner_headers = healthlabs_provision()
    healthlabs_set_capabilities(
        owner["userId"],
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.NUTRITION_WRITE,
    )
    food_name = f"Nutrition API food {uuid4()}"
    _, food_id = healthlabs_recipe_food(owner_headers, food_name)

    search = api_client.get(
        "/api/healthlabs/v1/nutrition/foods",
        params={"q": food_name, "country": "GB", "status": "unknown"},
        headers=owner_headers,
    )
    assert search.status_code == 200, search.text
    assert [item["id"] for item in search.json()["items"]] == [food_id]
    assert api_client.get(f"/api/healthlabs/v1/nutrition/foods/{food_id}", headers=owner_headers).status_code == 200

    source_id, nutrient_food_id = healthlabs_nutrition_source(name=food_name)
    resource = f"/api/healthlabs/v1/nutrition/foods/{food_id}/match"
    suggested = api_client.get(resource.removesuffix("/match"), params={"country": "GB"}, headers=owner_headers)
    assert suggested.status_code == 200, suggested.text
    assert suggested.json()["nutrition"]["reviewedBy"] is None
    assert suggested.json()["nutrition"]["appleNote"] is None
    body = {
        "nutrientFoodId": nutrient_food_id,
        "country": "GB",
        "confidence": 1,
        "foodGroup": "vegetable",
        "plantId": "test_plant",
        "allergens": {"gluten": "doesnt_contain"},
        "diets": {"vegan": "contains"},
    }
    request_headers = {
        **owner_headers,
        "Idempotency-Key": f"match-{uuid4()}",
        "X-HealthLabs-Access-Reason": "Curate source match",
    }
    matched = api_client.put(resource, json=body, headers=request_headers)
    replay = api_client.put(resource, json=body, headers=request_headers)
    assert matched.status_code == 200, matched.text
    assert replay.json() == matched.json()
    assert matched.json()["status"] == "matched"
    assert matched.json()["nutrition"]["nutrientFoodId"] == nutrient_food_id
    assert matched.json()["nutrition"]["foodGroup"] == "vegetable"

    _, replacement_food_id = healthlabs_nutrition_source(name=f"Replacement {uuid4()}", provider="replacement")
    replaced = api_client.put(
        resource,
        json={**body, "nutrientFoodId": replacement_food_id},
        headers={
            **owner_headers,
            "Idempotency-Key": f"replace-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Replace source match",
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["nutrition"]["nutrientFoodId"] == replacement_food_id

    portion = api_client.post(
        f"/api/healthlabs/v1/nutrition/foods/{food_id}/portions",
        json={"description": "one piece", "amount": 1, "unit": "piece", "grams": 80},
        headers={
            **owner_headers,
            "Idempotency-Key": f"portion-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Add measured portion",
        },
    )
    assert portion.status_code == 201, portion.text
    assert portion.json()["nutrition"]["portions"][0]["grams"] == 80

    cleared = api_client.delete(
        resource,
        headers={
            **owner_headers,
            "Idempotency-Key": f"clear-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Clear invalid match",
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] in {"review_required", "unknown"}
    assert cleared.json()["nutrition"] is None or cleared.json()["nutrition"]["id"] is None

    asyncio.run(
        NutritionReconciliationWorker(
            unfiltered_database.session,
            group_id=UUID(owner["groupId"]),
        ).run()
    )
    after_reconciliation = api_client.get(resource.removesuffix("/match"), headers=owner_headers)
    assert after_reconciliation.status_code == 200
    assert after_reconciliation.json()["status"] == "unknown"
    assert after_reconciliation.json()["nutrition"] is None

    unmatched_name = f"Suppressed nutrition food {uuid4()}"
    _, unmatched_food_id = healthlabs_recipe_food(owner_headers, unmatched_name)
    healthlabs_nutrition_source(name=unmatched_name)
    unmatched_resource = f"/api/healthlabs/v1/nutrition/foods/{unmatched_food_id}/match"
    suppressed = api_client.delete(
        unmatched_resource,
        headers={
            **owner_headers,
            "Idempotency-Key": f"clear-unmatched-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Reject automatic source match",
        },
    )
    assert suppressed.status_code == 200, suppressed.text
    asyncio.run(
        NutritionReconciliationWorker(
            unfiltered_database.session,
            group_id=UUID(owner["groupId"]),
        ).run()
    )
    after_unmatched_reconciliation = api_client.get(unmatched_resource.removesuffix("/match"), headers=owner_headers)
    assert after_unmatched_reconciliation.json()["status"] == "unknown"
    assert after_unmatched_reconciliation.json()["nutrition"] is None

    sources = api_client.get("/api/healthlabs/v1/nutrition/sources", headers=owner_headers)
    assert sources.status_code == 200
    assert source_id in {item["id"] for item in sources.json()}


def test_nutrition_mutations_reject_foods_from_another_workspace(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
    healthlabs_nutrition_source: Callable,
) -> None:
    _, owner_headers = healthlabs_provision()
    editor, editor_headers = healthlabs_provision()
    healthlabs_set_capabilities(
        editor["userId"],
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.NUTRITION_WRITE,
    )
    _, food_id = healthlabs_recipe_food(owner_headers, f"Foreign nutrition food {uuid4()}")
    _, nutrient_food_id = healthlabs_nutrition_source(name=f"Unrelated source {uuid4()}")
    resource = f"/api/healthlabs/v1/nutrition/foods/{food_id}"

    match = api_client.put(
        f"{resource}/match",
        json={"nutrientFoodId": nutrient_food_id, "country": "GB"},
        headers={
            **editor_headers,
            "Idempotency-Key": f"foreign-match-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Attempt foreign match",
        },
    )
    clear = api_client.delete(
        f"{resource}/match",
        headers={
            **editor_headers,
            "Idempotency-Key": f"foreign-clear-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Attempt foreign clear",
        },
    )
    portion = api_client.post(
        f"{resource}/portions",
        json={"description": "piece", "amount": 1, "unit": "item", "grams": 80},
        headers={
            **editor_headers,
            "Idempotency-Key": f"foreign-portion-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Attempt foreign portion",
        },
    )

    assert match.status_code == 404
    assert clear.status_code == 404
    assert portion.status_code == 404


def test_editor_portions_are_visible_and_calculated_only_in_their_workspace(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_nutrition_source: Callable,
) -> None:
    first, first_headers = healthlabs_provision()
    second, second_headers = healthlabs_provision()
    healthlabs_set_capabilities(
        first["userId"],
        HealthLabsCapability.RECIPES_READ_OWN,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.NUTRITION_WRITE,
    )
    healthlabs_set_capabilities(
        second["userId"],
        HealthLabsCapability.RECIPES_READ_OWN,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.NUTRITION_WRITE,
    )
    token = uuid4().hex
    unit_name = f"scoop-{token}"

    def create_recipe(headers: dict[str, str], food_name: str) -> tuple[str, str]:
        response = api_client.post(
            "/api/healthlabs/v1/recipes",
            headers={**headers, "Idempotency-Key": f"recipe-{uuid4()}"},
            json={
                "name": f"Recipe for {food_name}",
                "servings": 1,
                "ingredients": [
                    {
                        "quantity": 2,
                        "food": {"name": food_name},
                        "unit": {"name": unit_name, "abbreviation": unit_name},
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return body["id"], body["ingredients"][0]["food"]["id"]

    first_recipe_id, first_food_id = create_recipe(first_headers, f"First food {token}")
    second_recipe_id, second_food_id = create_recipe(second_headers, f"Second food {token}")
    _, nutrient_food_id = healthlabs_nutrition_source(name=f"Shared source {token}")

    for headers, food_id in ((first_headers, first_food_id), (second_headers, second_food_id)):
        response = api_client.put(
            f"/api/healthlabs/v1/nutrition/foods/{food_id}/match",
            json={"nutrientFoodId": nutrient_food_id, "country": "GB"},
            headers={
                **headers,
                "Idempotency-Key": f"match-{uuid4()}",
                "X-HealthLabs-Access-Reason": "Match shared source",
            },
        )
        assert response.status_code == 200, response.text

    first_portion = api_client.post(
        f"/api/healthlabs/v1/nutrition/foods/{first_food_id}/portions",
        json={"description": "scoop", "amount": 1, "unit": unit_name, "grams": 40},
        headers={
            **first_headers,
            "Idempotency-Key": f"portion-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Measure first workspace portion",
        },
    )
    assert first_portion.status_code == 201, first_portion.text

    first_detail = api_client.get(f"/api/healthlabs/v1/nutrition/foods/{first_food_id}", headers=first_headers).json()
    second_detail = api_client.get(
        f"/api/healthlabs/v1/nutrition/foods/{second_food_id}", headers=second_headers
    ).json()
    assert [portion["grams"] for portion in first_detail["nutrition"]["portions"]] == [40]
    assert second_detail["nutrition"]["portions"] == []

    second_before = api_client.get(f"/api/healthlabs/v1/recipes/{second_recipe_id}", headers=second_headers)
    assert second_before.status_code == 200, second_before.text
    assert second_before.json()["ingredients"][0]["nutrition"]["resolvedGrams"] is None

    second_portion = api_client.post(
        f"/api/healthlabs/v1/nutrition/foods/{second_food_id}/portions",
        json={"description": "scoop", "amount": 1, "unit": unit_name, "grams": 30},
        headers={
            **second_headers,
            "Idempotency-Key": f"portion-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Measure second workspace portion",
        },
    )
    assert second_portion.status_code == 201, second_portion.text

    first_recipe = api_client.get(f"/api/healthlabs/v1/recipes/{first_recipe_id}", headers=first_headers).json()
    second_recipe = api_client.get(f"/api/healthlabs/v1/recipes/{second_recipe_id}", headers=second_headers).json()
    assert first_recipe["ingredients"][0]["nutrition"]["resolvedGrams"] == 80
    assert second_recipe["ingredients"][0]["nutrition"]["resolvedGrams"] == 60


def test_source_food_search_is_tokenized_country_scoped_and_stable(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_nutrition_source: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    _, headers = healthlabs_provision()
    token = uuid4().hex[:10]
    gb_source_id, gb_food_id = healthlabs_nutrition_source(
        name=f"Oil, olive {token}", country="GB", provider=f"cofid-{token}"
    )
    _, other_food_id = healthlabs_nutrition_source(
        name=f"Olive {token} cooking oil",
        country="OTHER",
        provider=f"usda-{token}",
    )
    healthlabs_nutrition_source(name=f"Olive {token} oil", country="FR", provider=f"ciqual-{token}")
    unfiltered_database.session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=UUID(gb_food_id),
            description="tablespoon",
            amount=1,
            unit="tbsp",
            grams=14,
        )
    )
    unfiltered_database.session.commit()

    response = api_client.get(
        "/api/healthlabs/v1/nutrition/source-foods",
        params={"q": f"olive {token} oil", "country": "GB"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [gb_food_id, other_food_id]
    assert response.json()[0]["nutrientsPer100g"]["kcal"] == 50
    assert response.json()[0]["portions"] == [
        {"description": "tablespoon", "amount": 1.0, "unit": "tbsp", "grams": 14.0}
    ]
    assert response.json()[0]["source"]["id"] == gb_source_id
    assert response.json()[0]["source"]["country"] == "GB"
    assert len(response.json()[0]["source"]["sha256"]) == 64
    assert response.json()[1]["source"]["country"] == "OTHER"

    limited = api_client.get(
        "/api/healthlabs/v1/nutrition/source-foods",
        params={"q": token, "country": "GB", "limit": 1},
        headers=headers,
    )
    assert limited.status_code == 200
    assert [item["id"] for item in limited.json()] == [gb_food_id]

    punctuation = api_client.get(
        "/api/healthlabs/v1/nutrition/source-foods",
        params={"q": "---", "country": "GB"},
        headers=headers,
    )
    assert punctuation.status_code == 200
    assert punctuation.json() == []

    too_large = api_client.get(
        "/api/healthlabs/v1/nutrition/source-foods",
        params={"q": token, "country": "GB", "limit": 51},
        headers=headers,
    )
    assert too_large.status_code == 422


def test_source_food_search_prioritizes_food_names_and_measured_portions(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_nutrition_source: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    _, headers = healthlabs_provision()
    token = uuid4().hex[:10]
    healthlabs_nutrition_source(name=f"Creme egg {token}", country="GB")
    healthlabs_nutrition_source(name=f"Egg {token} boiled", country="GB")
    _, fried_id = healthlabs_nutrition_source(name=f"Egg {token} fried", country="OTHER")
    unfiltered_database.session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=UUID(fried_id),
            description="1 egg",
            amount=1,
            unit="egg",
            grams=55,
        )
    )
    unfiltered_database.session.commit()

    response = api_client.get(
        "/api/healthlabs/v1/nutrition/source-foods",
        params={"q": f"egg {token}", "country": "GB"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()] == [
        f"Egg {token} fried",
        f"Egg {token} boiled",
        f"Creme egg {token}",
    ]


def test_source_food_detail_returns_authoritative_nutrition_and_portions(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_nutrition_source: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    _, headers = healthlabs_provision()
    _, food_id = healthlabs_nutrition_source(name="Egg, whole, fried with oil", country="OTHER")
    unfiltered_database.session.add(
        HealthLabsFoodPortion(
            nutrient_food_id=UUID(food_id),
            description="1 egg",
            amount=1,
            unit="egg",
            grams=55,
        )
    )
    unfiltered_database.session.commit()

    response = api_client.get(
        f"/api/healthlabs/v1/nutrition/source-foods/{food_id}",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Egg, whole, fried with oil"
    assert response.json()["nutrientsPer100g"]["kcal"] == 50
    assert response.json()["portions"] == [{"description": "1 egg", "amount": 1.0, "unit": "egg", "grams": 55.0}]


def test_source_food_search_normalizes_accents_and_hides_editor_portions(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_nutrition_source: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    owner, headers = healthlabs_provision()
    token = uuid4().hex[:10]
    _, food_id = healthlabs_nutrition_source(
        name=f"Crème fraîche {token}",
        country="FR",
        provider=f"ciqual-{token}",
    )
    unfiltered_database.session.add_all(
        [
            HealthLabsFoodPortion(
                nutrient_food_id=UUID(food_id),
                description="source cup",
                amount=1,
                unit="cup",
                grams=240,
                source_managed=True,
            ),
            HealthLabsFoodPortion(
                nutrient_food_id=UUID(food_id),
                group_id=UUID(owner["groupId"]),
                description="editor scoop",
                amount=1,
                unit="scoop",
                grams=42,
                source_managed=False,
            ),
        ]
    )
    unfiltered_database.session.commit()

    response = api_client.get(
        "/api/healthlabs/v1/nutrition/source-foods",
        params={"q": f"fraiche creme {token}", "country": "FR"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [food_id]
    assert [portion["description"] for portion in response.json()[0]["portions"]] == ["source cup"]


def test_nutrition_mutations_require_headers_and_reject_stale_source(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
) -> None:
    editor, editor_headers = healthlabs_provision()
    healthlabs_set_capabilities(
        editor["userId"],
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.NUTRITION_WRITE,
    )
    _, food_id = healthlabs_recipe_food(editor_headers, f"Header food {uuid4()}")
    resource = f"/api/healthlabs/v1/nutrition/foods/{food_id}/match"
    body = {"nutrientFoodId": str(uuid4()), "country": "GB"}

    assert api_client.put(resource, json=body, headers=editor_headers).status_code == 422
    missing_reason = api_client.put(
        resource,
        json=body,
        headers={**editor_headers, "Idempotency-Key": f"missing-reason-{uuid4()}"},
    )
    assert missing_reason.status_code == 403
    stale = api_client.put(
        resource,
        json=body,
        headers={
            **editor_headers,
            "Idempotency-Key": f"stale-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Test stale source",
        },
    )
    assert stale.status_code == 404


def test_worker_status_and_review_queue_are_group_scoped(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    editor, editor_headers = healthlabs_provision()
    outsider, outsider_headers = healthlabs_provision()
    for identity in (editor, outsider):
        healthlabs_set_capabilities(
            identity["userId"],
            HealthLabsCapability.RECIPES_WRITE_OWN,
            HealthLabsCapability.NUTRITION_WRITE,
        )

    token = uuid4().hex
    recipe = api_client.post(
        "/api/healthlabs/v1/recipes",
        headers={**editor_headers, "Idempotency-Key": f"worker-recipe-{uuid4()}"},
        json={
            "name": f"Worker review {token}",
            "ingredients": [{"originalText": f"provider only {token}"}],
        },
    )
    assert recipe.status_code == 201, recipe.text

    session = unfiltered_database.session
    ingredient = session.scalar(
        sa.select(RecipeIngredientModel).where(RecipeIngredientModel.recipe_id == UUID(recipe.json()["id"]))
    )
    assert ingredient is not None
    work = None
    for _ in range(100):
        asyncio.run(NutritionReconciliationWorker(session, provider_daily_request_limit=0).run())
        work = session.scalar(
            sa.select(HealthLabsNutritionWork).where(
                HealthLabsNutritionWork.kind == "ingredient_structure",
                HealthLabsNutritionWork.target_id == str(ingredient.id),
            )
        )
        if work is not None:
            break
    assert work is not None
    status_response = api_client.get("/api/healthlabs/v1/nutrition/worker-status", headers=editor_headers)
    queue_response = api_client.get(
        "/api/healthlabs/v1/nutrition/work",
        params={"state": "review_required", "kind": "ingredient_structure"},
        headers=editor_headers,
    )
    outsider_queue = api_client.get("/api/healthlabs/v1/nutrition/work", headers=outsider_headers)

    assert status_response.status_code == 200, status_response.text
    assert status_response.json()["counts"]["review_required"] >= 1
    assert status_response.json()["providerEnabled"] is False
    assert status_response.json()["providerBudgetExhausted"] is False
    assert [item["id"] for item in queue_response.json()] == [str(work.id)]
    assert queue_response.json()[0]["result"] == work.result
    assert outsider_queue.status_code == 200
    assert outsider_queue.json() == []


def test_worker_status_and_review_queue_expose_only_sanitized_curator_state(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    editor, headers = healthlabs_provision()
    outsider, outsider_headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.NUTRITION_WRITE)
    healthlabs_set_capabilities(outsider["userId"], HealthLabsCapability.NUTRITION_WRITE)
    session = unfiltered_database.session
    group_id = UUID(editor["groupId"])
    agent = NutritionCuratorIdentityService(session).get_or_create(group_id)
    outsider_agent = NutritionCuratorIdentityService(session).get_or_create(UUID(outsider["groupId"]))
    work = HealthLabsNutritionWork(
        group_id=group_id,
        kind="food_match",
        target_id=str(uuid4()),
        input_sha256=f"{5:064x}",
        state="review_required",
        result={"reason": "no_unique_source_match"},
    )
    session.add(work)
    session.flush()
    now = get_utc_now()
    safe_note = f"I {'x' * 270}"
    for index, state in enumerate(("pending", "running", "approved", "failed", "insufficient_evidence")):
        session.add(
            HealthLabsNutritionCuratorDecision(
                work_id=work.id if state == "insufficient_evidence" else None,
                group_id=group_id,
                kind="food_match",
                target_id=work.target_id if state == "insufficient_evidence" else str(uuid4()),
                work_input_sha256=f"{index + 1:064x}",
                curator_sha256=f"{index + 11:064x}",
                agent_user_id=agent.id,
                state=state,
                attempt_count=index,
                candidate_snapshot={"recipeText": "PRIVATE RECIPE", "secret": "PRIVATE SECRET"},
                reason_code="insufficient_candidates",
                rationale=f"<b>{safe_note}</b>\x01",
                provider_name="test-provider",
                provider_model="test-model",
                prompt_version="test-v1",
                usage={"input_tokens": 999},
                update_at=now + timedelta(seconds=index),
            )
        )
    stale_work = HealthLabsNutritionWork(
        group_id=group_id,
        kind="food_match",
        target_id=str(uuid4()),
        input_sha256="f" * 64,
        state="review_required",
        result={"reason": "no_unique_source_match"},
    )
    session.add(stale_work)
    session.flush()
    session.add(
        HealthLabsNutritionCuratorDecision(
            work_id=stale_work.id,
            group_id=group_id,
            kind="food_match",
            target_id=stale_work.target_id,
            work_input_sha256="e" * 64,
            curator_sha256="f" * 64,
            agent_user_id=agent.id,
            state="failed",
            candidate_snapshot={"recipeText": "STALE PRIVATE RECIPE"},
            reason_code="provider_error",
            provider_name="stale-provider",
            provider_model="stale-model",
            prompt_version="stale-v1",
            update_at=now + timedelta(minutes=1),
        )
    )
    session.add(
        HealthLabsNutritionCuratorDecision(
            group_id=UUID(outsider["groupId"]),
            kind="food_match",
            target_id=str(uuid4()),
            work_input_sha256="d" * 64,
            curator_sha256="e" * 64,
            agent_user_id=outsider_agent.id,
            state="approved",
            candidate_snapshot={},
            reason_code="name_and_context_match",
            rationale="I selected an unrelated workspace source.",
            provider_name="outsider-provider",
            provider_model="outsider-model",
            prompt_version="outsider-v1",
            update_at=now + timedelta(minutes=5),
        )
    )
    session.commit()

    status = api_client.get("/api/healthlabs/v1/nutrition/worker-status", headers=headers)
    queue = api_client.get(
        "/api/healthlabs/v1/nutrition/work",
        params={"state": "review_required", "kind": "food_match"},
        headers=headers,
    )
    outsider_status = api_client.get("/api/healthlabs/v1/nutrition/worker-status", headers=outsider_headers)

    assert status.status_code == 200, status.text
    assert status.json()["curatorCounts"] == {
        "pending": 1,
        "running": 1,
        "approved": 1,
        "insufficient_evidence": 1,
        "failed": 2,
    }
    assert datetime.fromisoformat(status.json()["curatorLastUpdatedAt"]) == now + timedelta(minutes=1)
    assert outsider_status.json()["curatorCounts"]["approved"] == 1
    assert queue.status_code == 200, queue.text
    item = next(value for value in queue.json() if value["id"] == str(work.id))
    assert item["curatorState"] == "insufficient_evidence"
    assert item["curatorReasonCode"] == "insufficient_candidates"
    assert item["curatorAttemptCount"] == 4
    assert item["appleNote"] == safe_note
    assert item["curatorAgent"] == {
        "id": str(agent.id),
        "displayName": "Apple — Nutrition Curator",
        "userType": "agent",
    }
    stale_item = next(value for value in queue.json() if value["id"] == str(stale_work.id))
    assert stale_item["curatorState"] is None
    assert stale_item["curatorReasonCode"] is None
    assert stale_item["appleNote"] is None
    assert stale_item["curatorAgent"] is None
    for private_value in (
        "PRIVATE RECIPE",
        "PRIVATE SECRET",
        "input_tokens",
        "candidateSnapshot",
        "selectedIds",
        "test-provider",
        "test-model",
        "test-v1",
        "STALE PRIVATE RECIPE",
        "stale-provider",
        '"rationale"',
    ):
        assert private_value not in queue.text


def test_food_match_exposes_apple_as_agent_reviewer(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
    healthlabs_nutrition_source: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    editor, headers = healthlabs_provision()
    healthlabs_set_capabilities(
        editor["userId"],
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.NUTRITION_WRITE,
    )
    group_id = UUID(editor["groupId"])
    _, food_id = healthlabs_recipe_food(headers, f"Apple reviewed food {uuid4()}")
    _, nutrient_food_id = healthlabs_nutrition_source(name=f"Apple source {uuid4()}")
    session = unfiltered_database.session
    agent = NutritionCuratorIdentityService(session).get_or_create(group_id)
    match = HealthLabsFoodMatch(
        ingredient_food_id=UUID(food_id),
        nutrient_food_id=UUID(nutrient_food_id),
        country="GB",
        match_method="agent",
        confidence=0.98,
        auto_applied=False,
        reviewed_by_user_id=agent.id,
        reviewed_at=get_utc_now(),
    )
    session.add(match)
    now = get_utc_now()
    for curator_sha256, rationale, updated_at in (
        ("d" * 64, "I selected an older source.", now - timedelta(minutes=1)),
        ("c" * 64, "I selected the closest licensed source.", now),
    ):
        session.add(
            HealthLabsNutritionCuratorDecision(
                group_id=group_id,
                kind="food_match",
                target_id=food_id,
                work_input_sha256="b" * 64,
                curator_sha256=curator_sha256,
                agent_user_id=agent.id,
                state="approved",
                candidate_snapshot={},
                selected_ids={"nutrient_food_id": nutrient_food_id},
                confidence=0.98,
                runner_up_confidence=0.3,
                reason_code="name_and_context_match",
                rationale=rationale,
                provider_name="test-provider",
                provider_model="test-model",
                prompt_version="test-v1",
                update_at=updated_at,
            )
        )
    session.commit()

    response = api_client.get(f"/api/healthlabs/v1/nutrition/foods/{food_id}", headers=headers)

    assert response.status_code == 200, response.text
    nutrition = response.json()["nutrition"]
    assert nutrition["reviewedByUserId"] == str(agent.id)
    assert nutrition["reviewedBy"] == {
        "id": str(agent.id),
        "displayName": "Apple — Nutrition Curator",
        "userType": "agent",
    }
    assert nutrition["appleNote"] == "I selected the closest licensed source."


def test_worker_review_queue_is_bounded(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    editor, headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.NUTRITION_WRITE)
    session = unfiltered_database.session
    session.add_all(
        [
            HealthLabsNutritionWork(
                group_id=UUID(editor["groupId"]),
                kind="food_match",
                target_id=str(uuid4()),
                input_sha256=str(index).zfill(64),
                state="review_required",
                result={"reason": "no_unique_source_match"},
            )
            for index in range(3)
        ]
    )
    session.commit()

    response = api_client.get(
        "/api/healthlabs/v1/nutrition/work",
        params={"state": "review_required", "limit": 2},
        headers=headers,
    )

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert (
        api_client.get(
            "/api/healthlabs/v1/nutrition/work",
            params={"state": "review_required", "limit": 101},
            headers=headers,
        ).status_code
        == 422
    )


def test_worker_status_reports_configured_exhausted_provider_without_exposing_other_groups(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    unfiltered_database: AllRepositories,
    monkeypatch: MonkeyPatch,
) -> None:
    editor, headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.NUTRITION_WRITE)
    app_settings = get_app_settings()
    monkeypatch.setattr(app_settings, "HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT", 1)
    monkeypatch.setattr(app_settings, "HEALTHLABS_NUTRITION_PROVIDER_DAILY_BUDGET_USD", Decimal("1"))
    monkeypatch.setattr(
        app_settings,
        "HEALTHLABS_NUTRITION_PROVIDER_INPUT_USD_PER_MILLION_TOKENS",
        Decimal("1"),
    )
    session = unfiltered_database.session
    settings = session.scalar(
        sa.select(AIProviderSettings).where(AIProviderSettings.group_id == UUID(editor["groupId"]))
    )
    assert settings is not None
    provider = AIProvider(
        session=session,
        settings_id=settings.id,
        name=f"status-provider-{uuid4()}",
        base_url="https://openrouter.example/api/v1",
        api_key="test-runtime-key",
        model="qwen/test",
        timeout=30,
    )
    session.add(provider)
    session.flush()
    settings.default_provider_id = provider.id
    usage = session.scalar(
        sa.select(HealthLabsNutritionProviderUsage).where(
            HealthLabsNutritionProviderUsage.usage_date == get_utc_now().date()
        )
    )
    if usage is None:
        usage = HealthLabsNutritionProviderUsage(usage_date=get_utc_now().date(), request_count=1)
        session.add(usage)
    else:
        usage.request_count = 1
    session.commit()

    response = api_client.get("/api/healthlabs/v1/nutrition/worker-status", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["providerEnabled"] is True
    assert response.json()["providerBudgetExhausted"] is True
    assert "providerRequestsToday" not in response.json()
