from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mealie.core.config import get_app_settings
from mealie.core.security import create_access_token, hash_password
from mealie.core.security.providers.openid_provider import OpenIDProvider
from mealie.db.db_setup import session_context
from mealie.db.models.group import Group
from mealie.db.models.healthlabs import HealthLabsCapabilityGrant, HealthLabsNutritionCuratorDecision
from mealie.db.models.household import Household
from mealie.db.models.users import LongLiveToken, PasswordResetModel, User, UserType
from mealie.repos.repository_factory import AllRepositories
from mealie.services.healthlabs.nutrition_curator_identity_service import NutritionCuratorIdentityService
from tests.utils import api_routes
from tests.utils.factories import random_string
from tests.utils.fixture_schemas import TestUser


def test_get_or_create_provisions_one_least_privilege_agent_in_oldest_household(
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    current_household = session.get(Household, unique_user_fn_scoped._household_id)
    assert current_household is not None
    current_household.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    oldest = Household(
        session=session,
        id=UUID("00000000-0000-4000-8000-000000000001"),
        group_id=unique_user_fn_scoped._group_id,
        name=f"oldest-{uuid4()}",
        slug=f"oldest-{uuid4()}",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    session.add(oldest)
    session.commit()

    first = NutritionCuratorIdentityService(session).get_or_create(unique_user_fn_scoped._group_id)
    second = NutritionCuratorIdentityService(session).get_or_create(unique_user_fn_scoped._group_id)

    assert second.id == first.id
    assert first.user_type is UserType.AGENT
    assert first.healthlabs_managed is True
    assert first.external_subject_id == f"healthlabs:agent:nutrition-curator:{unique_user_fn_scoped.group_id}"
    assert first.username == f"nutrition-curator-{UUID(unique_user_fn_scoped.group_id).hex}"
    assert first.email == f"{UUID(unique_user_fn_scoped.group_id).hex}@nutrition-curator.invalid"
    assert first.full_name == "Apple — Nutrition Curator"
    assert first.household_id == oldest.id
    assert first.admin is False
    assert first.can_manage is False
    assert first.can_manage_household is False
    assert first.can_invite is False
    assert first.can_organize is False

    assert session.scalar(sa.select(sa.func.count()).select_from(User).where(User.id == first.id)) == 1
    grants = session.scalars(
        sa.select(HealthLabsCapabilityGrant).where(HealthLabsCapabilityGrant.user_id == first.id)
    ).all()
    assert [(grant.capability, grant.revoked_at) for grant in grants] == [("nutrition:write", None)]
    assert (
        session.scalar(sa.select(sa.func.count()).select_from(LongLiveToken).where(LongLiveToken.user_id == first.id))
        == 0
    )


def test_concurrent_get_or_create_returns_one_agent(unique_user_fn_scoped: TestUser) -> None:
    group_id = unique_user_fn_scoped._group_id

    def provision() -> UUID:
        with session_context() as session:
            return NutritionCuratorIdentityService(session).get_or_create(group_id).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        agent_ids = list(executor.map(lambda _: provision(), range(2)))

    assert agent_ids[0] == agent_ids[1]
    with session_context() as session:
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(User)
                .where(User.external_subject_id == f"healthlabs:agent:nutrition-curator:{group_id}")
            )
            == 1
        )


def test_get_or_create_does_not_restore_revoked_grant_during_active_decision(
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    agent = NutritionCuratorIdentityService(session).get_or_create(unique_user_fn_scoped._group_id)
    grant = session.scalar(sa.select(HealthLabsCapabilityGrant).where(HealthLabsCapabilityGrant.user_id == agent.id))
    assert grant is not None
    grant.revoked_at = datetime.now(UTC)
    session.add(
        HealthLabsNutritionCuratorDecision(
            group_id=unique_user_fn_scoped._group_id,
            kind="food_match",
            target_id=str(uuid4()),
            work_input_sha256="1" * 64,
            curator_sha256="2" * 64,
            agent_user_id=agent.id,
            state="running",
            candidate_snapshot={},
            provider_name="test",
            provider_model="test",
            prompt_version="test",
        )
    )
    session.commit()

    NutritionCuratorIdentityService(session).get_or_create(unique_user_fn_scoped._group_id)

    grants = session.scalars(
        sa.select(HealthLabsCapabilityGrant).where(HealthLabsCapabilityGrant.user_id == agent.id)
    ).all()
    assert len(grants) == 1
    assert grants[0].revoked_at is not None


def test_agent_cannot_authenticate_or_use_an_already_signed_jwt(
    api_client: TestClient,
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    agent = NutritionCuratorIdentityService(session).get_or_create(unique_user_fn_scoped._group_id)
    known_password = "known-agent-password"
    known_hash = hash_password(known_password)
    session.get(User, agent.id).password = known_hash
    session.commit()

    response = api_client.post(
        api_routes.auth_token,
        data={"username": agent.email, "password": known_password},
    )
    assert response.status_code == 401

    with pytest.raises(HTTPException) as oidc_error:
        OpenIDProvider(
            session,
            {
                "sub": agent.external_subject_id,
                "preferred_username": agent.username,
                "email": agent.email,
                "email_verified": True,
                "name": agent.full_name,
            },
        ).authenticate()
    assert oidc_error.value.status_code == 403

    signed_token, _ = create_access_token({"sub": str(agent.id)})
    headers = {"Authorization": f"Bearer {signed_token}"}
    assert api_client.get(api_routes.users_self, headers=headers).status_code == 403
    assert (
        api_client.put(
            api_routes.users_password,
            json={"currentPassword": known_password, "newPassword": "replacement-password"},
            headers=headers,
        ).status_code
        == 403
    )
    assert api_client.post(api_routes.users_api_tokens, json={"name": "forbidden"}, headers=headers).status_code == 403
    session.expire_all()
    assert session.get(User, agent.id).password == known_hash
    assert (
        session.scalar(sa.select(sa.func.count()).select_from(LongLiveToken).where(LongLiveToken.user_id == agent.id))
        == 0
    )


def test_oidc_rejects_agent_before_profile_mutation(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = unique_user_fn_scoped.repos.session
    agent = NutritionCuratorIdentityService(session).get_or_create(unique_user_fn_scoped._group_id)
    monkeypatch.setenv("OIDC_REQUIRES_GROUP_CLAIM", "true")
    monkeypatch.setenv("OIDC_GROUPS_CLAIM", "groups")
    monkeypatch.setenv("OIDC_ADMIN_GROUP", "admins")
    get_app_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as error:
            OpenIDProvider(
                session,
                {
                    "sub": agent.external_subject_id,
                    "preferred_username": agent.username,
                    "email": agent.email,
                    "email_verified": True,
                    "name": agent.full_name,
                    "groups": ["admins"],
                    "picture": "https://images.example/apple.png",
                },
            ).authenticate()
        assert error.value.status_code == 403
        session.expire_all()
        unchanged = session.get(User, agent.id)
        assert unchanged is not None
        assert unchanged.admin is False
        assert unchanged.external_avatar_hash is None
    finally:
        get_app_settings.cache_clear()


def test_admin_update_cannot_change_human_user_type(
    api_client: TestClient,
    admin_token: dict[str, str],
    unique_user_fn_scoped: TestUser,
) -> None:
    response = api_client.get(
        api_routes.admin_users_item_id(unique_user_fn_scoped.user_id),
        headers=admin_token,
    )
    assert response.status_code == 200
    update = response.json()
    update["userType"] = "agent"

    response = api_client.put(
        api_routes.admin_users_item_id(unique_user_fn_scoped.user_id),
        headers=admin_token,
        json=update,
    )

    assert response.status_code == 200
    assert response.json()["userType"] == "human"
    unique_user_fn_scoped.repos.session.expire_all()
    assert unique_user_fn_scoped.repos.session.get(User, unique_user_fn_scoped.user_id).user_type is UserType.HUMAN


def test_password_reset_and_admin_user_management_cannot_mutate_agent(
    api_client: TestClient,
    admin_token: dict[str, str],
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    agent = NutritionCuratorIdentityService(session).get_or_create(unique_user_fn_scoped._group_id)
    original = (agent.full_name, agent.group_id, agent.household_id, agent.password)

    assert api_client.post("/api/users/forgot-password", json={"email": agent.email}).status_code == 200
    assert (
        session.scalar(
            sa.select(sa.func.count()).select_from(PasswordResetModel).where(PasswordResetModel.user_id == agent.id)
        )
        == 0
    )
    reset_token = f"agent-reset-{uuid4()}"
    session.add(PasswordResetModel(user_id=agent.id, token=reset_token))
    session.commit()
    response = api_client.post(
        api_routes.users_reset_password,
        json={
            "token": reset_token,
            "email": agent.email,
            "password": "replacement-password",
            "passwordConfirm": "replacement-password",
        },
    )
    assert response.status_code == 403

    assert (
        api_client.post(
            api_routes.admin_users_password_reset_token,
            json={"email": agent.email},
            headers=admin_token,
        ).status_code
        == 403
    )
    agent_data = api_client.get(api_routes.admin_users_item_id(agent.id), headers=admin_token).json()
    agent_data["fullName"] = "Renamed agent"
    assert (
        api_client.put(api_routes.admin_users_item_id(agent.id), json=agent_data, headers=admin_token).status_code
        == 403
    )
    destination = api_client.post(api_routes.admin_groups, json={"name": random_string()}, headers=admin_token)
    assert destination.status_code == 201
    agent_data["fullName"] = agent.full_name
    agent_data["group"] = destination.json()["name"]
    agent_data["household"] = destination.json()["households"][0]["name"]
    assert (
        api_client.put(api_routes.admin_users_item_id(agent.id), json=agent_data, headers=admin_token).status_code
        == 403
    )
    assert api_client.delete(api_routes.admin_users_item_id(agent.id), headers=admin_token).status_code == 403

    session.expire_all()
    unchanged = session.get(User, agent.id)
    assert unchanged is not None
    assert (unchanged.full_name, unchanged.group_id, unchanged.household_id, unchanged.password) == original


def test_group_deletion_removes_its_managed_agent(
    api_client: TestClient,
    admin_token: dict[str, str],
    unfiltered_database: AllRepositories,
) -> None:
    response = api_client.post(api_routes.admin_groups, json={"name": random_string()}, headers=admin_token)
    assert response.status_code == 201
    group_id = UUID(response.json()["id"])
    agent = NutritionCuratorIdentityService(unfiltered_database.session).get_or_create(group_id)
    source = HealthLabsNutritionCuratorDecision(
        group_id=group_id,
        kind="food_match",
        target_id=str(uuid4()),
        work_input_sha256="3" * 64,
        curator_sha256="4" * 64,
        reuse_sha256="5" * 64,
        agent_user_id=agent.id,
        state="approved",
        candidate_snapshot={},
        provider_name="test",
        provider_model="test",
        prompt_version="test",
    )
    unfiltered_database.session.add(source)
    unfiltered_database.session.flush()
    unfiltered_database.session.add(
        HealthLabsNutritionCuratorDecision(
            group_id=group_id,
            kind="food_match",
            target_id=str(uuid4()),
            work_input_sha256="3" * 64,
            curator_sha256="6" * 64,
            reuse_sha256="5" * 64,
            reused_from_decision_id=source.id,
            agent_user_id=agent.id,
            state="approved",
            candidate_snapshot={},
            provider_name="test",
            provider_model="test",
            prompt_version="test",
        )
    )
    unfiltered_database.session.commit()

    response = api_client.delete(api_routes.admin_groups_item_id(group_id), headers=admin_token)

    assert response.status_code == 200
    unfiltered_database.session.expire_all()
    assert unfiltered_database.session.get(Group, group_id) is None
    assert unfiltered_database.session.get(User, agent.id) is None
    assert (
        unfiltered_database.session.scalar(
            sa.select(sa.func.count())
            .select_from(HealthLabsNutritionCuratorDecision)
            .where(HealthLabsNutritionCuratorDecision.group_id == group_id)
        )
        == 0
    )
