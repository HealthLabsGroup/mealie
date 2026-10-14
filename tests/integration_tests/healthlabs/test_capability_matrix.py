import hashlib
from collections.abc import Callable
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.core.config import get_app_settings
from mealie.db.models.healthlabs import HealthLabsAccessAudit, HealthLabsCapabilityGrant
from mealie.db.models.users import User
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability


def _set_capabilities(
    database: AllRepositories,
    user_id: str,
    *capabilities: HealthLabsCapability,
    admin: bool = False,
) -> None:
    session = database.session
    user = session.get(User, user_id)
    assert user is not None
    user.admin = admin
    session.execute(sa.delete(HealthLabsCapabilityGrant).where(HealthLabsCapabilityGrant.user_id == user_id))
    session.add_all(
        [
            HealthLabsCapabilityGrant(
                user_id=user_id,
                capability=capability.value,
                reason="capability matrix test",
            )
            for capability in capabilities
        ]
    )
    session.commit()


def _create(api_client: TestClient, headers: dict[str, str], name: str, *, scope: str = "personal"):
    return api_client.post(
        "/api/healthlabs/v1/recipes",
        json={"name": name, "scope": scope, "ingredients": [], "instructions": [{"text": "Cook"}]},
        headers={**headers, "Idempotency-Key": f"create-{uuid4()}"},
    )


def test_catalogue_nutritionist_editor_and_admin_capabilities_are_separate(
    api_client: TestClient,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, owner_headers = healthlabs_provision()
    nutritionist, nutritionist_headers = healthlabs_provision()
    platform_admin, admin_headers = healthlabs_provision()
    personal = _create(api_client, owner_headers, f"Private {uuid4()}")
    assert personal.status_code == 201

    secret = "catalogue-matrix-secret"
    monkeypatch.setattr(get_app_settings(), "HEALTHLABS_PROVISIONING_SECRET", secret)
    secret_headers = {"X-HealthLabs-Provisioning-Secret": secret}
    reader_response = api_client.post("/api/healthlabs/v1/catalogue", json={"access": "reader"}, headers=secret_headers)
    editor_response = api_client.post("/api/healthlabs/v1/catalogue", json={"access": "editor"}, headers=secret_headers)
    assert reader_response.status_code in {200, 201}
    assert editor_response.status_code in {200, 201}
    reader_headers = {"Authorization": f"Bearer {reader_response.json()['token']}"}
    editor_headers = {"Authorization": f"Bearer {editor_response.json()['token']}"}

    catalogue = _create(api_client, editor_headers, f"Catalogue {uuid4()}", scope="catalogue")
    assert catalogue.status_code == 201
    assert (
        api_client.get(f"/api/healthlabs/v1/recipes/{catalogue.json()['id']}", headers=reader_headers).status_code
        == 200
    )
    reader_write = api_client.put(
        f"/api/healthlabs/v1/recipes/{catalogue.json()['id']}",
        json={"name": "Reader edit", "scope": "catalogue", "ingredients": [], "instructions": []},
        headers={**reader_headers, "Idempotency-Key": f"reader-{uuid4()}"},
    )
    assert reader_write.status_code == 403
    assert (
        api_client.get(f"/api/healthlabs/v1/recipes/{personal.json()['id']}", headers=editor_headers).status_code == 404
    )

    _set_capabilities(
        unfiltered_database,
        nutritionist["userId"],
        HealthLabsCapability.RECIPES_READ_ALL,
    )
    cross_read = api_client.get(
        f"/api/healthlabs/v1/recipes/{personal.json()['id']}",
        headers={**nutritionist_headers, "X-HealthLabs-Access-Reason": "Care review"},
    )
    assert cross_read.status_code == 200
    assert _create(api_client, nutritionist_headers, f"Denied {uuid4()}").status_code == 403

    audit = unfiltered_database.session.scalar(
        sa.select(HealthLabsAccessAudit).where(HealthLabsAccessAudit.actor_user_id == nutritionist["userId"])
    )
    assert audit is not None
    owner_pseudonym = "sha256:" + hashlib.sha256(owner["externalSubjectId"].encode()).hexdigest()
    assert audit.subject_external_id == owner_pseudonym
    assert audit.reason == "Care review"
    with pytest.raises(ValueError, match="immutable"):
        audit.reason = "Rewritten reason"
        unfiltered_database.session.commit()
    unfiltered_database.session.rollback()

    _set_capabilities(unfiltered_database, platform_admin["userId"], admin=True)
    denied = api_client.get(f"/api/healthlabs/v1/recipes/{personal.json()['id']}", headers=admin_headers)
    assert denied.status_code == 403
    break_glass = api_client.get(
        f"/api/healthlabs/v1/recipes/{personal.json()['id']}",
        headers={**admin_headers, "X-HealthLabs-Break-Glass-Reason": "Incident 123"},
    )
    assert break_glass.status_code == 200
