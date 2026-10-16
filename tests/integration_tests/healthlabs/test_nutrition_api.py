from collections.abc import Callable
from uuid import uuid4

from fastapi.testclient import TestClient

from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_food_search_detail_match_replace_portion_and_clear(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
    healthlabs_nutrition_source: Callable,
) -> None:
    owner, owner_headers = healthlabs_provision()
    editor, editor_headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.NUTRITION_WRITE)
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
        **editor_headers,
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
            **editor_headers,
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
            **editor_headers,
            "Idempotency-Key": f"portion-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Add measured portion",
        },
    )
    assert portion.status_code == 201, portion.text
    assert portion.json()["nutrition"]["portions"][0]["grams"] == 80

    cleared = api_client.delete(
        resource,
        headers={
            **editor_headers,
            "Idempotency-Key": f"clear-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Clear invalid match",
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] in {"review_required", "unknown"}
    assert cleared.json()["nutrition"] is None or cleared.json()["nutrition"]["id"] is None

    sources = api_client.get("/api/healthlabs/v1/nutrition/sources", headers=owner_headers)
    assert sources.status_code == 200
    assert source_id in {item["id"] for item in sources.json()}


def test_nutrition_mutations_require_headers_and_reject_stale_source(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
) -> None:
    owner, owner_headers = healthlabs_provision()
    editor, editor_headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.NUTRITION_WRITE)
    _, food_id = healthlabs_recipe_food(owner_headers, f"Header food {uuid4()}")
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
