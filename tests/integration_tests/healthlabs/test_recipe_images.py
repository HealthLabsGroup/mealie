from collections.abc import Callable
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mealie.core.config import get_app_settings
from mealie.services.healthlabs.recipe_api_service import MAX_IMAGE_BYTES
from tests import data


def _create(api_client: TestClient, headers: dict[str, str], name: str, *, scope: str = "personal") -> dict:
    response = api_client.post(
        "/api/healthlabs/v1/recipes",
        json={"name": name, "scope": scope, "ingredients": [], "instructions": [{"text": "Cook"}]},
        headers={**headers, "Idempotency-Key": f"create-{uuid4()}"},
    )
    assert response.status_code == 201
    return response.json()


def _upload(
    api_client: TestClient,
    headers: dict[str, str],
    recipe_id: str,
    image: bytes,
    key: str,
    content_type: str = "image/jpeg",
):
    return api_client.post(
        f"/api/healthlabs/v1/recipes/{recipe_id}/image",
        files={"image": ("recipe", image, content_type)},
        headers={**headers, "Idempotency-Key": key},
    )


def test_image_create_read_replace_delete_revision_and_replay(
    api_client: TestClient,
    healthlabs_provision: Callable,
) -> None:
    _, headers = healthlabs_provision()
    recipe = _create(api_client, headers, f"Image recipe {uuid4()}")

    first_key = f"image-{uuid4()}"
    first = _upload(api_client, headers, recipe["id"], data.images_test_image_1.read_bytes(), first_key)
    replay = _upload(api_client, headers, recipe["id"], data.images_test_image_1.read_bytes(), first_key)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["revisionNumber"] == recipe["revisionNumber"] + 1

    read = api_client.get(f"/api/healthlabs/v1/recipes/{recipe['id']}/image", headers=headers)
    assert read.status_code == 200
    assert read.headers["content-type"] == "image/webp"
    assert read.content

    replaced = _upload(
        api_client,
        headers,
        recipe["id"],
        data.images_test_image_2.read_bytes(),
        f"image-{uuid4()}",
        "image/png",
    )
    assert replaced.status_code == 200
    assert replaced.json()["revisionNumber"] == first.json()["revisionNumber"] + 1

    deleted = api_client.delete(f"/api/healthlabs/v1/recipes/{recipe['id']}/image", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["revisionNumber"] == replaced.json()["revisionNumber"] + 1
    assert api_client.get(f"/api/healthlabs/v1/recipes/{recipe['id']}/image", headers=headers).status_code == 404


def test_image_limits_and_workspace_policy(
    api_client: TestClient,
    healthlabs_provision: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner_headers = healthlabs_provision()
    _, other_headers = healthlabs_provision()
    recipe = _create(api_client, owner_headers, f"Private image {uuid4()}")

    too_large = _upload(api_client, owner_headers, recipe["id"], b"x" * (MAX_IMAGE_BYTES + 1), f"large-{uuid4()}")
    assert too_large.status_code == 413
    invalid_type = api_client.post(
        f"/api/healthlabs/v1/recipes/{recipe['id']}/image",
        files={"image": ("recipe.txt", b"not an image", "text/plain")},
        headers={**owner_headers, "Idempotency-Key": f"type-{uuid4()}"},
    )
    assert invalid_type.status_code == 415
    assert (
        _upload(
            api_client,
            other_headers,
            recipe["id"],
            data.images_test_image_1.read_bytes(),
            f"cross-{uuid4()}",
        ).status_code
        == 404
    )
    assert api_client.get(f"/api/healthlabs/v1/recipes/{recipe['id']}/image", headers=other_headers).status_code == 404

    secret = f"catalogue-image-{uuid4()}"
    monkeypatch.setattr(get_app_settings(), "HEALTHLABS_PROVISIONING_SECRET", secret)
    secret_headers = {"X-HealthLabs-Provisioning-Secret": secret}
    reader = api_client.post("/api/healthlabs/v1/catalogue", json={"access": "reader"}, headers=secret_headers)
    editor = api_client.post("/api/healthlabs/v1/catalogue", json={"access": "editor"}, headers=secret_headers)
    reader_headers = {"Authorization": f"Bearer {reader.json()['token']}"}
    editor_headers = {"Authorization": f"Bearer {editor.json()['token']}"}
    catalogue = _create(api_client, editor_headers, f"Catalogue image {uuid4()}", scope="catalogue")
    assert (
        _upload(
            api_client,
            reader_headers,
            catalogue["id"],
            data.images_test_image_1.read_bytes(),
            f"reader-{uuid4()}",
        ).status_code
        == 403
    )
