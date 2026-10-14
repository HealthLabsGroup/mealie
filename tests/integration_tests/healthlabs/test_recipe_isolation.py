from collections.abc import Callable
from uuid import uuid4

from fastapi.testclient import TestClient


def _payload(name: str) -> dict:
    return {
        "name": name,
        "ingredients": [
            {
                "referenceId": str(uuid4()),
                "quantity": 1,
                "food": {"name": "Shared-name apple"},
                "unit": {"name": "piece"},
            }
        ],
        "instructions": [{"text": "Eat it."}],
    }


def _create(api_client: TestClient, headers: dict[str, str], name: str):
    return api_client.post(
        "/api/healthlabs/v1/recipes",
        json=_payload(name),
        headers={**headers, "Idempotency-Key": f"create-{uuid4()}"},
    )


def test_personal_workspaces_and_ingredient_entities_are_isolated(
    api_client: TestClient,
    healthlabs_provision: Callable,
) -> None:
    _, alice_headers = healthlabs_provision()
    _, bob_headers = healthlabs_provision()
    alice = _create(api_client, alice_headers, f"Alice apple {uuid4()}")
    bob = _create(api_client, bob_headers, f"Bob apple {uuid4()}")
    assert alice.status_code == bob.status_code == 201

    alice_recipe = alice.json()
    bob_recipe = bob.json()
    assert alice_recipe["ingredients"][0]["food"]["id"] != bob_recipe["ingredients"][0]["food"]["id"]
    assert alice_recipe["ingredients"][0]["unit"]["id"] != bob_recipe["ingredients"][0]["unit"]["id"]

    assert api_client.get(f"/api/healthlabs/v1/recipes/{bob_recipe['id']}", headers=alice_headers).status_code == 404
    denied_update = api_client.put(
        f"/api/healthlabs/v1/recipes/{bob_recipe['id']}",
        json=_payload("Stolen"),
        headers={**alice_headers, "Idempotency-Key": f"update-{uuid4()}"},
    )
    assert denied_update.status_code == 404
    assert api_client.get(f"/api/healthlabs/v1/recipes/{alice_recipe['id']}", headers=alice_headers).status_code == 200
