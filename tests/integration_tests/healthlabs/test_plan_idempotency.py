from collections.abc import Callable
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mealie.db.models.recipe.recipe import RecipeModel
from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_publication_and_cooked_mutations_are_idempotent_and_optimistic(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_plan_payload: Callable,
    session: Session,
) -> None:
    subject, subject_headers = healthlabs_provision()
    editor, editor_headers = healthlabs_provision()
    recipe = api_client.post(
        "/api/healthlabs/v1/recipes",
        json={"name": f"Cooked recipe {uuid4()}", "ingredients": [], "instructions": []},
        headers={**subject_headers, "Idempotency-Key": f"recipe-{uuid4()}"},
    )
    assert recipe.status_code == 201
    healthlabs_set_capabilities(
        editor["userId"],
        HealthLabsCapability.PLANS_WRITE_ALL,
        HealthLabsCapability.RECIPES_READ_ALL,
    )
    body = healthlabs_plan_payload(
        recipe_id=recipe.json()["id"],
        recipe_revision_id=recipe.json()["revisionId"],
    )
    publish_key = f"publish-{uuid4()}"
    publish_headers = {
        **editor_headers,
        "Idempotency-Key": publish_key,
        "X-HealthLabs-Access-Reason": "Nutrition care plan",
    }
    path = f"/api/healthlabs/v1/subjects/{subject['externalSubjectId']}/plan-publications"
    assert (
        api_client.post(
            path,
            json=body,
            headers={**editor_headers, "X-HealthLabs-Access-Reason": "Nutrition care plan"},
        ).status_code
        == 422
    )
    first = api_client.post(path, json=body, headers=publish_headers)
    replay = api_client.post(path, json=body, headers=publish_headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    changed = {**body, "days": 7}
    assert api_client.post(path, json=changed, headers=publish_headers).status_code == 409

    plan = first.json()
    entry = plan["entries"][0]
    patch_path = f"/api/healthlabs/v1/plans/{plan['id']}/entries/{entry['id']}"
    assert api_client.patch(patch_path, json={"version": 1, "cooked": True}, headers=subject_headers).status_code == 422
    assert (
        api_client.patch(
            patch_path,
            json={"version": 1, "cooked": True, "date": body["from"]},
            headers={**subject_headers, "Idempotency-Key": f"structural-{uuid4()}"},
        ).status_code
        == 422
    )
    patch_headers = {**subject_headers, "Idempotency-Key": f"cooked-{uuid4()}"}
    cooked = api_client.patch(patch_path, json={"version": 1, "cooked": True}, headers=patch_headers)
    cooked_replay = api_client.patch(patch_path, json={"version": 1, "cooked": True}, headers=patch_headers)
    assert cooked.status_code == cooked_replay.status_code == 200
    assert cooked.json() == cooked_replay.json()
    assert cooked.json()["version"] == 2
    assert cooked.json()["cooked"] is True
    assert cooked.json()["cookedAt"] is not None
    assert api_client.patch(patch_path, json={"version": 1, "cooked": False}, headers=patch_headers).status_code == 409
    stale_headers = {**subject_headers, "Idempotency-Key": f"stale-{uuid4()}"}
    assert api_client.patch(patch_path, json={"version": 1, "cooked": False}, headers=stale_headers).status_code == 409
    assert session.get(RecipeModel, recipe.json()["id"]).last_made is None
