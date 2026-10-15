from collections.abc import Callable

import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.models.healthlabs import HealthLabsCapabilityGrant
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_subject_provisioning_grants_only_own_plan_capabilities(
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    subject, _ = healthlabs_provision()
    capabilities = set(subject["capabilities"])
    assert HealthLabsCapability.PLANS_READ_OWN in capabilities
    assert HealthLabsCapability.PLANS_MARK_COOKED_OWN in capabilities
    assert HealthLabsCapability.PLANS_WRITE_ALL not in capabilities
    assert HealthLabsCapability.RECIPES_READ_ALL not in capabilities

    unfiltered_database.session.execute(
        sa.delete(HealthLabsCapabilityGrant).where(
            HealthLabsCapabilityGrant.user_id == subject["userId"],
            HealthLabsCapabilityGrant.capability.in_(
                [
                    HealthLabsCapability.PLANS_READ_OWN.value,
                    HealthLabsCapability.PLANS_MARK_COOKED_OWN.value,
                ]
            ),
        )
    )
    unfiltered_database.session.commit()
    reprovisioned, _ = healthlabs_provision(subject["externalSubjectId"])
    assert HealthLabsCapability.PLANS_READ_OWN in reprovisioned["capabilities"]
    assert HealthLabsCapability.PLANS_MARK_COOKED_OWN in reprovisioned["capabilities"]


def test_role_admin_and_revoked_grants_do_not_authorize_plan_access(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
) -> None:
    subject, _ = healthlabs_provision()
    administrator, admin_headers = healthlabs_provision()
    revoked, revoked_headers = healthlabs_provision()
    healthlabs_set_capabilities(administrator["userId"], admin=True)
    healthlabs_set_capabilities(
        revoked["userId"],
        HealthLabsCapability.PLANS_WRITE_ALL,
        revoked=True,
    )
    path = f"/api/healthlabs/v1/subjects/{subject['externalSubjectId']}/plans/current"
    headers = {"X-HealthLabs-Access-Reason": "Nutrition care plan"}
    assert api_client.get(path, headers={**admin_headers, **headers}).status_code == 403
    assert api_client.get(path, headers={**revoked_headers, **headers}).status_code == 403


def test_revoked_own_plan_grants_take_effect_on_next_request(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
) -> None:
    subject, headers = healthlabs_provision()
    assert api_client.get("/api/healthlabs/v1/plans/current", headers=headers).status_code == 200
    healthlabs_set_capabilities(
        subject["userId"],
        HealthLabsCapability.PLANS_READ_OWN,
        HealthLabsCapability.PLANS_MARK_COOKED_OWN,
        revoked=True,
    )
    assert api_client.get("/api/healthlabs/v1/plans/current", headers=headers).status_code == 403
