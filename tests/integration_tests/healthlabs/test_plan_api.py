from collections.abc import Callable
from uuid import uuid4

from fastapi.testclient import TestClient

from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_editor_publishes_and_reads_subject_plan_while_subject_reads_only_own(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_plan_payload: Callable,
) -> None:
    subject, subject_headers = healthlabs_provision()
    other, other_headers = healthlabs_provision()
    editor, editor_headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.PLANS_WRITE_ALL)

    published = api_client.post(
        f"/api/healthlabs/v1/subjects/{subject['externalSubjectId']}/plan-publications",
        json=healthlabs_plan_payload(),
        headers={
            **editor_headers,
            "Idempotency-Key": f"publish-{uuid4()}",
            "X-HealthLabs-Access-Reason": "Nutrition care plan",
        },
    )
    assert published.status_code == 201
    assert api_client.get("/api/healthlabs/v1/plans/current", headers=subject_headers).json() == published.json()
    plan = published.json()
    entry = plan["entries"][0]
    assert (
        api_client.patch(
            f"/api/healthlabs/v1/plans/{plan['id']}/entries/{entry['id']}",
            json={"version": 1, "cooked": True},
            headers={**other_headers, "Idempotency-Key": f"other-cooked-{uuid4()}"},
        ).status_code
        == 404
    )
    assert (
        api_client.get(
            f"/api/healthlabs/v1/subjects/{subject['externalSubjectId']}/plans/current",
            headers={**editor_headers, "X-HealthLabs-Access-Reason": "Nutrition care plan"},
        ).json()
        == published.json()
    )
    assert (
        api_client.get(
            f"/api/healthlabs/v1/subjects/{other['externalSubjectId']}/plans/current",
            headers={**subject_headers, "X-HealthLabs-Access-Reason": "Not permitted"},
        ).status_code
        == 403
    )
    assert (
        api_client.get(
            "/api/healthlabs/v1/subjects/missing-subject/plans/current",
            headers={**editor_headers, "X-HealthLabs-Access-Reason": "Nutrition care plan"},
        ).status_code
        == 404
    )


def test_recipe_owner_metadata_is_only_resolved_for_cross_subject_reader(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
) -> None:
    owner, owner_headers = healthlabs_provision()
    editor, editor_headers = healthlabs_provision()
    recipe_name = f"Owner recipe {uuid4()}"
    recipe = api_client.post(
        "/api/healthlabs/v1/recipes",
        json={"name": recipe_name, "ingredients": [], "instructions": []},
        headers={**owner_headers, "Idempotency-Key": f"recipe-{uuid4()}"},
    )
    assert recipe.status_code == 201
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.RECIPES_READ_ALL)

    own_item = api_client.get("/api/healthlabs/v1/recipes", headers=owner_headers).json()["items"][0]
    assert own_item.get("ownerExternalSubjectId") is None
    response = api_client.get(
        "/api/healthlabs/v1/recipes",
        params={"q": recipe_name},
        headers={**editor_headers, "X-HealthLabs-Access-Reason": "Build meal plan"},
    )
    assert response.status_code == 200
    item = next(item for item in response.json()["items"] if item["id"] == recipe.json()["id"])
    assert item["ownerExternalSubjectId"] == owner["externalSubjectId"]
