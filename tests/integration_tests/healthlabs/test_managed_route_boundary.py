from uuid import uuid4

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from mealie.core.config import get_app_settings
from tests.utils import TestUser


def test_managed_token_is_confined_to_healthlabs_routes(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
    monkeypatch: MonkeyPatch,
) -> None:
    secret = "managed-boundary-test-secret"
    monkeypatch.setattr(get_app_settings(), "HEALTHLABS_PROVISIONING_SECRET", secret)
    provisioned = api_client.post(
        "/api/healthlabs/v1/subjects",
        json={"externalSubjectId": f"boundary-{uuid4()}"},
        headers={"X-HealthLabs-Provisioning-Secret": secret},
    )
    assert provisioned.status_code == 201
    managed_headers = {"Authorization": f"Bearer {provisioned.json()['token']}"}

    for method, path in (
        ("GET", "/api/recipes"),
        ("GET", "/api/households/mealplans"),
        ("GET", "/api/groups/self"),
        ("POST", "/api/users/api-tokens"),
        ("GET", "/api/admin/users"),
    ):
        assert api_client.request(method, path, json={"name": "Bypass"}, headers=managed_headers).status_code == 403

    assert api_client.get("/api/recipes", headers=unique_user_fn_scoped.token).status_code == 200
    assert api_client.get("/api/groups/self", headers=unique_user_fn_scoped.token).status_code == 200
