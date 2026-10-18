from collections.abc import Callable
from uuid import uuid4

from fastapi.testclient import TestClient

from mealie.db.models.users import User
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_nutrition_read_and_write_capabilities_are_independent(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
    healthlabs_nutrition_source: Callable,
    unfiltered_database: AllRepositories,
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

    session = unfiltered_database.session
    owner_user = session.get(User, owner["userId"])
    assert owner_user is not None
    for identity in (ordinary, catalogue_editor, plan_editor, nutrition_editor, admin):
        user = session.get(User, identity["userId"])
        assert user is not None
        user.group_id = owner_user.group_id
    session.commit()

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


def test_worker_status_and_review_queue_require_nutrition_write(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
) -> None:
    ordinary, ordinary_headers = healthlabs_provision()
    editor, editor_headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.NUTRITION_WRITE)

    for resource in ("worker-status", "work"):
        assert api_client.get(f"/api/healthlabs/v1/nutrition/{resource}", headers=ordinary_headers).status_code == 403
        allowed = api_client.get(f"/api/healthlabs/v1/nutrition/{resource}", headers=editor_headers)
        assert allowed.status_code == 200
        if resource == "worker-status":
            assert allowed.json()["counts"] == {
                "pending": 0,
                "running": 0,
                "review_required": 0,
                "completed": 0,
                "failed": 0,
            }
            assert allowed.json()["curatorCounts"] == {
                "pending": 0,
                "running": 0,
                "approved": 0,
                "insufficient_evidence": 0,
                "failed": 0,
            }
