from collections.abc import Callable

from fastapi.testclient import TestClient

from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_current_capabilities_returns_only_active_grants(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
) -> None:
    editor, editor_headers = healthlabs_provision()
    administrator, administrator_headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.NUTRITION_WRITE)
    healthlabs_set_capabilities(
        administrator["userId"],
        HealthLabsCapability.NUTRITION_WRITE,
        admin=True,
        revoked=True,
    )

    editor_response = api_client.get("/api/healthlabs/v1/capabilities", headers=editor_headers)
    administrator_response = api_client.get("/api/healthlabs/v1/capabilities", headers=administrator_headers)

    assert editor_response.status_code == 200, editor_response.text
    assert editor_response.json() == ["nutrition:write"]
    assert administrator_response.status_code == 200, administrator_response.text
    assert administrator_response.json() == []
