from collections.abc import Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi.testclient import TestClient


def _recipe_payload(name: str, *, description: str = "Fresh and simple") -> dict:
    tomato_ref = str(uuid4())
    basil_ref = str(uuid4())
    return {
        "name": name,
        "description": description,
        "scope": "personal",
        "servings": 2,
        "ingredients": [
            {
                "referenceId": tomato_ref,
                "quantity": 125.5,
                "food": {"name": "Tomato"},
                "unit": {"name": "gram", "abbreviation": "g"},
                "originalText": "125.5 g tomato",
            },
            {
                "referenceId": basil_ref,
                "quantity": 4,
                "food": {"name": "Basil"},
                "unit": {"name": "leaf", "pluralName": "leaves"},
                "originalText": "4 basil leaves",
            },
        ],
        "instructions": [
            {"title": "Prep", "text": "Slice the tomatoes.", "ingredientReferences": [tomato_ref]},
            {"title": "Finish", "text": "Add basil.", "ingredientReferences": [basil_ref]},
        ],
        "authoring": {"quantitiesConfirmed": True},
    }


def test_recipe_crud_search_revision_and_exact_structured_round_trip(
    api_client: TestClient,
    healthlabs_provision: Callable,
) -> None:
    _, headers = healthlabs_provision()
    name = f"Tomato basil {uuid4()}"
    payload = _recipe_payload(name)

    created = api_client.post(
        "/api/healthlabs/v1/recipes",
        json=payload,
        headers={**headers, "Idempotency-Key": f"create-{uuid4()}"},
    )
    assert created.status_code == 201
    recipe = created.json()
    assert recipe["name"] == name
    assert recipe["scope"] == "personal"
    assert recipe["revisionNumber"] == 1
    assert recipe["revisionId"]
    assert recipe["authoring"] == payload["authoring"]
    assert [ingredient["referenceId"] for ingredient in recipe["ingredients"]] == [
        ingredient["referenceId"] for ingredient in payload["ingredients"]
    ]
    assert [step["text"] for step in recipe["instructions"]] == [step["text"] for step in payload["instructions"]]

    fetched = api_client.get(f"/api/healthlabs/v1/recipes/{recipe['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json() == recipe

    search = api_client.get("/api/healthlabs/v1/recipes", params={"q": name}, headers=headers)
    assert search.status_code == 200
    assert [item["id"] for item in search.json()["items"]] == [recipe["id"]]

    first_revision = api_client.get(
        f"/api/healthlabs/v1/recipes/{recipe['id']}/revisions/{recipe['revisionId']}",
        headers=headers,
    )
    assert first_revision.status_code == 200
    assert first_revision.json()["recipe"]["name"] == name

    updated_payload = _recipe_payload(name, description="Updated description")
    updated = api_client.put(
        f"/api/healthlabs/v1/recipes/{recipe['id']}",
        json=updated_payload,
        headers={**headers, "Idempotency-Key": f"update-{uuid4()}"},
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "Updated description"
    assert updated.json()["revisionNumber"] == 2

    second_name = f"Second tomato {uuid4()}"
    second = api_client.post(
        "/api/healthlabs/v1/recipes",
        json=_recipe_payload(second_name),
        headers={**headers, "Idempotency-Key": f"create-{uuid4()}"},
    )
    assert second.status_code == 201
    assert second.json()["ingredients"][0]["food"]["id"] == recipe["ingredients"][0]["food"]["id"]
    assert second.json()["ingredients"][0]["unit"]["id"] == recipe["ingredients"][0]["unit"]["id"]

    deleted = api_client.delete(
        f"/api/healthlabs/v1/recipes/{recipe['id']}",
        headers={**headers, "Idempotency-Key": f"delete-{uuid4()}"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["id"] == recipe["id"]
    assert api_client.get(f"/api/healthlabs/v1/recipes/{recipe['id']}", headers=headers).status_code == 404


def test_recipe_accepts_deterministic_ingredient_references_in_instructions(
    api_client: TestClient,
    healthlabs_provision: Callable,
) -> None:
    _, headers = healthlabs_provision()
    reference_id = str(uuid5(NAMESPACE_URL, "ephra:recipe:ingredient:0"))
    payload = {
        "name": f"Deterministic references {uuid4()}",
        "ingredients": [
            {
                "referenceId": reference_id,
                "quantity": 1,
                "food": {"name": "Pearl barley"},
            }
        ],
        "instructions": [
            {
                "text": "Cook the barley.",
                "ingredientReferences": [reference_id],
            }
        ],
    }

    created = api_client.post(
        "/api/healthlabs/v1/recipes",
        json=payload,
        headers={**headers, "Idempotency-Key": f"create-{uuid4()}"},
    )

    assert created.status_code == 201, created.text
    assert created.json()["ingredients"][0]["referenceId"] == reference_id
    assert created.json()["instructions"][0]["ingredientReferences"] == [reference_id]
