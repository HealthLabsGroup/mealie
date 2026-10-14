from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.models.healthlabs import HealthLabsIdempotencyOperation
from mealie.db.models.recipe import RecipeModel
from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.repos.repository_factory import AllRepositories


def _payload(name: str) -> dict:
    return {"name": name, "ingredients": [], "instructions": [{"text": "Done"}]}


def test_same_key_replays_and_changed_request_conflicts(
    api_client: TestClient,
    healthlabs_provision: Callable,
) -> None:
    _, headers = healthlabs_provision()
    key = f"same-{uuid4()}"
    request_headers = {**headers, "Idempotency-Key": key}
    payload = _payload(f"Idempotent {uuid4()}")

    first = api_client.post("/api/healthlabs/v1/recipes", json=payload, headers=request_headers)
    replay = api_client.post("/api/healthlabs/v1/recipes", json=payload, headers=request_headers)
    conflict = api_client.post(
        "/api/healthlabs/v1/recipes",
        json=_payload(f"Different {uuid4()}"),
        headers=request_headers,
    )

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    operation = api_client.get(f"/api/healthlabs/v1/operations/{key}", headers=headers)
    assert operation.status_code == 200
    assert operation.json()["state"] == "completed"
    assert operation.json()["responseStatus"] == 201
    assert operation.json()["responseBody"] == first.json()
    assert operation.json()["completedAt"] is not None


def test_mutation_requires_nonempty_idempotency_key(
    api_client: TestClient,
    healthlabs_provision: Callable,
) -> None:
    _, headers = healthlabs_provision()
    payload = _payload(f"Missing key {uuid4()}")

    missing = api_client.post("/api/healthlabs/v1/recipes", json=payload, headers=headers)
    empty = api_client.post(
        "/api/healthlabs/v1/recipes",
        json=payload,
        headers={**headers, "Idempotency-Key": ""},
    )

    assert missing.status_code == 422
    assert empty.status_code == 422


def test_concurrent_same_key_creates_one_recipe(
    api_client: TestClient,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    _, headers = healthlabs_provision()
    key = f"concurrent-{uuid4()}"
    name = f"Concurrent recipe {uuid4()}"
    request_headers = {**headers, "Idempotency-Key": key}

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _: api_client.post(
                    "/api/healthlabs/v1/recipes",
                    json=_payload(name),
                    headers=request_headers,
                ),
                range(2),
            )
        )

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    count = unfiltered_database.session.scalar(
        sa.select(sa.func.count()).select_from(RecipeModel).where(RecipeModel.name == name)
    )
    assert count == 1


def test_completed_operation_expires_after_24_hours(
    api_client: TestClient,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    provisioned, headers = healthlabs_provision()
    key = f"expired-{uuid4()}"
    request_headers = {**headers, "Idempotency-Key": key}
    first = api_client.post(
        "/api/healthlabs/v1/recipes",
        json=_payload(f"Before expiry {uuid4()}"),
        headers=request_headers,
    )
    assert first.status_code == 201

    operation = unfiltered_database.session.scalar(
        sa.select(HealthLabsIdempotencyOperation).where(
            HealthLabsIdempotencyOperation.actor_user_id == provisioned["userId"],
            HealthLabsIdempotencyOperation.idempotency_key == key,
        )
    )
    assert operation is not None
    operation.expires_at = get_utc_now() - timedelta(seconds=1)
    unfiltered_database.session.commit()

    after_expiry = api_client.post(
        "/api/healthlabs/v1/recipes",
        json=_payload(f"After expiry {uuid4()}"),
        headers=request_headers,
    )
    assert after_expiry.status_code == 201
    assert after_expiry.json()["id"] != first.json()["id"]
