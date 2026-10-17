from collections.abc import Callable
from uuid import uuid4

from fastapi.testclient import TestClient

from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_nutrition_read_and_write_capabilities_are_independent(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
    healthlabs_nutrition_source: Callable,
) -> None:
    owner, owner_headers = healthlabs_provision()
    ordinary, ordinary_headers = healthlabs_provision()
    catalogue_editor, catalogue_headers = healthlabs_provision()
    plan_editor, plan_headers = healthlabs_provision()
    nutrition_editor, nutrition_headers = healthlabs_provision()
    admin, admin_headers = healthlabs_provision()
    _, food_id = healthlabs_recipe_food(owner_headers, f"Capability food {uuid4()}")
    source_name = f"Capability source {uuid4()}"
    _, nutrient_food_id = healthlabs_nutrition_source(name=source_name)
    resource = f"/api/healthlabs/v1/nutrition/foods/{food_id}"
    match_resource = f"{resource}/match"
    body = {"nutrientFoodId": nutrient_food_id, "country": "GB"}

    assert api_client.get(resource, headers=owner_headers).status_code == 200
    assert api_client.get(resource, headers=ordinary_headers).status_code == 404
    source_search = api_client.get(
        "/api/healthlabs/v1/nutrition/source-foods",
        params={"q": source_name, "country": "GB"},
        headers=ordinary_headers,
    )
    assert source_search.status_code == 200
    assert [item["id"] for item in source_search.json()] == [nutrient_food_id]

    healthlabs_set_capabilities(catalogue_editor["userId"], HealthLabsCapability.CATALOGUE_WRITE)
    healthlabs_set_capabilities(plan_editor["userId"], HealthLabsCapability.PLANS_WRITE_ALL)
    healthlabs_set_capabilities(nutrition_editor["userId"], HealthLabsCapability.NUTRITION_WRITE)
    healthlabs_set_capabilities(admin["userId"], admin=True)

    for headers in (ordinary_headers, catalogue_headers, plan_headers):
        denied = api_client.put(
            match_resource,
            json=body,
            headers={
                **headers,
                "Idempotency-Key": f"denied-{uuid4()}",
                "X-HealthLabs-Access-Reason": "Not a nutrition editor",
            },
        )
        assert denied.status_code == 403

    allowed = api_client.put(
        match_resource,
        json=body,
        headers={
            **nutrition_headers,
            "Idempotency-Key": f"allowed-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Nutrition review",
        },
    )
    assert allowed.status_code == 200, allowed.text

    denied_admin = api_client.delete(
        match_resource,
        headers={
            **admin_headers,
            "Idempotency-Key": f"admin-denied-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Admin without break glass",
        },
    )
    assert denied_admin.status_code == 403
    break_glass = api_client.delete(
        match_resource,
        headers={
            **admin_headers,
            "Idempotency-Key": f"admin-break-glass-{uuid4()}",
            "X-HealthLabs-Break-Glass-Reason": "Incident 456",
        },
    )
    assert break_glass.status_code == 200, break_glass.text
