from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from mealie.db.models.healthlabs import HealthLabsAccessAudit, HealthLabsCapabilityGrant
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.services.healthlabs.access_policy import (
    BreakGlassContext,
    RecipeAccessDenied,
    RecipeAccessPolicy,
)
from tests.utils import TestUser

PRINCIPAL_CAPABILITIES = {
    "recipe_user": {
        HealthLabsCapability.RECIPES_READ_OWN,
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.PLANS_READ_OWN,
        HealthLabsCapability.PLANS_MARK_COOKED_OWN,
        HealthLabsCapability.CATALOGUE_READ,
    },
    "nutritionist": {
        HealthLabsCapability.RECIPES_READ_ALL,
        HealthLabsCapability.PLANS_WRITE_ALL,
        HealthLabsCapability.CATALOGUE_READ,
    },
    "catalogue_editor": {
        HealthLabsCapability.CATALOGUE_READ,
        HealthLabsCapability.CATALOGUE_WRITE,
    },
    "platform_admin": set(),
}


def _grant(user: TestUser, capability: HealthLabsCapability, *, revoked: bool = False) -> None:
    user.repos.session.add(
        HealthLabsCapabilityGrant(
            user_id=user.user_id,
            capability=capability.value,
            reason="test grant",
            revoked_at=datetime.now(UTC) if revoked else None,
        )
    )
    user.repos.session.commit()


@pytest.mark.parametrize(("principal", "capabilities"), PRINCIPAL_CAPABILITIES.items())
def test_capability_matrix_denies_by_default_and_uses_explicit_grants(
    unique_user_fn_scoped: TestUser,
    principal: str,
    capabilities: set[HealthLabsCapability],
) -> None:
    for capability in capabilities:
        _grant(unique_user_fn_scoped, capability)

    user = unique_user_fn_scoped.repos.users.get_one(unique_user_fn_scoped.user_id)
    assert user is not None
    if principal == "platform_admin":
        user = user.model_copy(update={"admin": True})
    policy = RecipeAccessPolicy(unique_user_fn_scoped.repos.session, user)

    assert policy.active_capabilities() == capabilities
    for capability in HealthLabsCapability:
        assert policy.allows(capability) is (capability in capabilities)


def test_combined_grants_exclude_revoked_capabilities(unique_user_fn_scoped: TestUser) -> None:
    _grant(unique_user_fn_scoped, HealthLabsCapability.RECIPES_READ_OWN)
    _grant(unique_user_fn_scoped, HealthLabsCapability.CATALOGUE_READ)
    _grant(unique_user_fn_scoped, HealthLabsCapability.RECIPES_WRITE_OWN, revoked=True)
    user = unique_user_fn_scoped.repos.users.get_one(unique_user_fn_scoped.user_id)
    assert user is not None

    policy = RecipeAccessPolicy(unique_user_fn_scoped.repos.session, user)

    assert policy.active_capabilities() == {
        HealthLabsCapability.RECIPES_READ_OWN,
        HealthLabsCapability.CATALOGUE_READ,
    }


def test_admin_has_no_content_shortcut_and_break_glass_requires_an_audit_reason(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped.repos.users.get_one(unique_user_fn_scoped.user_id)
    assert user is not None
    admin = user.model_copy(update={"admin": True})
    policy = RecipeAccessPolicy(unique_user_fn_scoped.repos.session, admin)

    assert not policy.allows(HealthLabsCapability.RECIPES_READ_ALL)
    with pytest.raises(RecipeAccessDenied):
        policy.require(HealthLabsCapability.RECIPES_READ_ALL)
    with pytest.raises(RecipeAccessDenied):
        policy.require(
            HealthLabsCapability.RECIPES_READ_ALL,
            break_glass=BreakGlassContext(action="recipe.read_all", reason="   "),
        )

    policy.require(
        HealthLabsCapability.RECIPES_READ_ALL,
        break_glass=BreakGlassContext(
            action="recipe.read_all",
            reason="production incident 42",
            subject_external_id="subject-123",
            resource_type="recipe",
            resource_id="recipe-456",
        ),
    )
    audit = unique_user_fn_scoped.repos.session.execute(
        select(HealthLabsAccessAudit).where(HealthLabsAccessAudit.actor_user_id == unique_user_fn_scoped.user_id)
    ).scalar_one()
    assert audit.action == "recipe.read_all"
    assert audit.reason == "production incident 42"
    assert audit.subject_external_id == "subject-123"
    assert audit.resource_type == "recipe"
    assert audit.resource_id == "recipe-456"
    unique_user_fn_scoped.repos.session.commit()


def test_non_admin_cannot_use_break_glass(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped.repos.users.get_one(unique_user_fn_scoped.user_id)
    assert user is not None
    policy = RecipeAccessPolicy(unique_user_fn_scoped.repos.session, user)

    with pytest.raises(RecipeAccessDenied):
        policy.require(
            HealthLabsCapability.CATALOGUE_WRITE,
            break_glass=BreakGlassContext(
                action="catalogue.write",
                reason="not an administrator",
            ),
        )
