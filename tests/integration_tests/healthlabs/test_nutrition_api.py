from collections.abc import Callable
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from mealie.db.models.healthlabs import HealthLabsFoodPortion
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_food_search_detail_match_replace_portion_and_clear(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
    healthlabs_nutrition_source: Callable,
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
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.NUTRITION_WRITE)
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
