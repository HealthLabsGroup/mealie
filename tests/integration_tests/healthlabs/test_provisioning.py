from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient
from pytest import MonkeyPatch, fixture

from mealie.core.config import get_app_settings
from mealie.db.models.group import Group
from mealie.db.models.healthlabs import HealthLabsCapabilityGrant
from mealie.db.models.household import Household
from mealie.db.models.users import LongLiveToken, User
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability

PROVISIONING_SECRET = "integration-test-provisioning-secret"
PROVISIONING_HEADERS = {"X-HealthLabs-Provisioning-Secret": PROVISIONING_SECRET}
SUBJECTS_URL = "/api/healthlabs/v1/subjects"
CATALOGUE_URL = "/api/healthlabs/v1/catalogue"


@fixture(autouse=True)
def provisioning_secret(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(get_app_settings(), "HEALTHLABS_PROVISIONING_SECRET", PROVISIONING_SECRET)


def _provision(api_client: TestClient, subject: str):
    return api_client.post(
        SUBJECTS_URL,
        json={"externalSubjectId": subject},
        headers=PROVISIONING_HEADERS,
    )


def test_subject_provisioning_is_secret_authenticated_and_idempotent(
    api_client: TestClient,
    unfiltered_database: AllRepositories,
) -> None:
    assert api_client.post(SUBJECTS_URL, json={"externalSubjectId": f"subject-{uuid4()}"}).status_code == 401

    subject = f"subject-{uuid4()}"
    first = _provision(api_client, subject)
    second = _provision(api_client, subject)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    body = first.json()
    assert body["externalSubjectId"] == subject
    assert body["token"]
    assert body["capabilities"] == [
        HealthLabsCapability.CATALOGUE_READ.value,
        HealthLabsCapability.RECIPES_READ_OWN.value,
        HealthLabsCapability.RECIPES_WRITE_OWN.value,
    ]

    session = unfiltered_database.session
    user = session.scalar(sa.select(User).where(User.external_subject_id == subject))
    assert user is not None
    assert user.healthlabs_managed is True
    assert user.email.endswith("@subjects.invalid")
    assert session.scalar(sa.select(sa.func.count()).select_from(User).where(User.external_subject_id == subject)) == 1
    assert session.scalar(sa.select(sa.func.count()).select_from(Group).where(Group.id == user.group_id)) == 1
    assert (
        session.scalar(sa.select(sa.func.count()).select_from(Household).where(Household.id == user.household_id)) == 1
    )
    assert (
        session.scalar(sa.select(sa.func.count()).select_from(LongLiveToken).where(LongLiveToken.user_id == user.id))
        == 1
    )


def test_concurrent_subject_retry_creates_one_workspace(api_client: TestClient) -> None:
    subject = f"concurrent-{uuid4()}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: _provision(api_client, subject), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json() == responses[1].json()


def test_catalogue_reader_and_editor_have_separate_capabilities(api_client: TestClient) -> None:
    reader = api_client.post(CATALOGUE_URL, json={"access": "reader"}, headers=PROVISIONING_HEADERS)
    editor = api_client.post(CATALOGUE_URL, json={"access": "editor"}, headers=PROVISIONING_HEADERS)

    assert reader.status_code in {200, 201}
    assert editor.status_code in {200, 201}
    assert reader.json()["groupId"] == editor.json()["groupId"]
    assert reader.json()["householdId"] == editor.json()["householdId"]
    assert reader.json()["userId"] != editor.json()["userId"]
    assert reader.json()["capabilities"] == [HealthLabsCapability.CATALOGUE_READ.value]
    assert editor.json()["capabilities"] == [
        HealthLabsCapability.CATALOGUE_READ.value,
        HealthLabsCapability.CATALOGUE_WRITE.value,
    ]


def test_rotation_invalidates_prior_token_and_deprovision_removes_workspace(
    api_client: TestClient,
    unfiltered_database: AllRepositories,
) -> None:
    subject = f"rotate-{uuid4()}"
    provisioned = _provision(api_client, subject)
    assert provisioned.status_code == 201
    old_token = provisioned.json()["token"]
    old_headers = {"Authorization": f"Bearer {old_token}"}

    rotated = api_client.post(
        f"{SUBJECTS_URL}/{subject}/tokens/rotate",
        headers=PROVISIONING_HEADERS,
    )
    assert rotated.status_code == 200
    assert rotated.json()["token"] != old_token
    assert api_client.get("/api/recipes", headers=old_headers).status_code == 401

    new_headers = {"Authorization": f"Bearer {rotated.json()['token']}"}
    assert api_client.get("/api/healthlabs/v1/recipes", headers=new_headers).status_code == 404

    deleted = api_client.delete(f"{SUBJECTS_URL}/{subject}", headers=PROVISIONING_HEADERS)
    assert deleted.status_code == 204
    session = unfiltered_database.session
    session.expire_all()
    assert session.scalar(sa.select(User).where(User.external_subject_id == subject)) is None
    assert api_client.get("/api/recipes", headers=new_headers).status_code == 401


def test_provisioning_grants_are_active_and_exact(
    api_client: TestClient,
    unfiltered_database: AllRepositories,
) -> None:
    subject = f"grants-{uuid4()}"
    response = _provision(api_client, subject)
    assert response.status_code == 201
    user_id = response.json()["userId"]

    grants = unfiltered_database.session.scalars(
        sa.select(HealthLabsCapabilityGrant)
        .where(HealthLabsCapabilityGrant.user_id == user_id)
        .where(HealthLabsCapabilityGrant.revoked_at.is_(None))
    ).all()
    assert {grant.capability for grant in grants} == {
        HealthLabsCapability.CATALOGUE_READ.value,
        HealthLabsCapability.RECIPES_READ_OWN.value,
        HealthLabsCapability.RECIPES_WRITE_OWN.value,
    }
