from collections.abc import Callable
from datetime import date
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from mealie.db.models.healthlabs import (
    HealthLabsCapabilityGrant,
    HealthLabsIdempotencyOperation,
    HealthLabsPlanPublication,
    HealthLabsPlanPublicationEntry,
)
from mealie.db.models.household.mealplan import GroupMealPlan
from mealie.db.models.recipe.recipe import HealthLabsRecipeRevision, RecipeModel
from mealie.db.models.users import User
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.healthlabs.plan import HealthLabsPlanEntryWrite, HealthLabsPlanPublicationWrite
from mealie.schema.recipe.recipe import Recipe
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.idempotency_service import IdempotencyConflict
from mealie.services.healthlabs.plan_api_service import (
    HealthLabsPlanConflict,
    HealthLabsPlanNotFound,
    PlanApiService,
)
from tests.utils import TestUser

START = date(2026, 10, 15)


def _editor(user: TestUser, *, recipe_access: bool = True) -> PrivateUser:
    capabilities = [HealthLabsCapability.PLANS_WRITE_ALL]
    if recipe_access:
        capabilities.append(HealthLabsCapability.RECIPES_READ_ALL)
    user.repos.session.add_all(
        [
            HealthLabsCapabilityGrant(
                user_id=user.user_id,
                capability=capability.value,
                reason="Plan service test",
            )
            for capability in capabilities
        ]
    )
    user.repos.session.commit()
    editor = user.repos.users.get_one(user.user_id)
    assert editor is not None
    return editor


def _target(
    healthlabs_provision: Callable,
    repositories: AllRepositories,
) -> User:
    provisioned, _ = healthlabs_provision()
    target = repositories.session.get(User, UUID(provisioned["userId"]))
    assert target is not None
    return target


def _recipe_revision(user: TestUser, *, name: str = "Plan soup") -> tuple[RecipeModel, HealthLabsRecipeRevision]:
    recipe = user.repos.recipes.create(
        Recipe(
            user_id=user.user_id,
            group_id=user.group_id,
            name=f"{name} {uuid4()}",
            recipe_servings=4,
        )
    )
    model = user.repos.session.get(RecipeModel, recipe.id)
    assert model is not None
    revision = HealthLabsRecipeRevision(
        recipe_id=recipe.id,
        revision_number=1,
        snapshot={"id": str(recipe.id), "name": recipe.name, "recipe_servings": 4},
        content_sha256="a" * 64,
        actor_user_id=user.user_id,
    )
    user.repos.session.add(revision)
    user.repos.session.commit()
    return model, revision


def _write(
    revision: HealthLabsRecipeRevision,
    *,
    expected_publication_id: UUID | None = None,
    entry_id: UUID | None = None,
    title: str | None = None,
) -> HealthLabsPlanPublicationWrite:
    return HealthLabsPlanPublicationWrite(
        expected_publication_id=expected_publication_id,
        from_date=START,
        days=7,
        entries=[
            HealthLabsPlanEntryWrite(
                id=entry_id or uuid4(),
                date=START,
                entry_type="dinner",
                position=0,
                servings=2,
                recipe_id=revision.recipe_id,
                recipe_revision_id=revision.id,
                title=title,
            )
        ],
    )


def test_publish_replays_exactly_numbers_monotonically_and_replaces_only_managed_rows(
    unique_user_fn_scoped: TestUser,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    session = unfiltered_database.session
    editor = _editor(unique_user_fn_scoped)
    target = _target(healthlabs_provision, unfiltered_database)
    _, revision = _recipe_revision(unique_user_fn_scoped)
    ordinary = GroupMealPlan(
        session=session,
        date=START,
        entry_type="breakfast",
        title="Ordinary row",
        text="",
        group_id=target.group_id,
        user_id=target.id,
    )
    session.add(ordinary)
    session.commit()

    service = PlanApiService(session, editor)
    first_write = _write(revision)
    first = service.publish(
        target.external_subject_id or "",
        first_write,
        idempotency_key=f"publish-{uuid4()}",
        access_reason="Preparing the patient's plan",
    )
    replay = service.publish(
        target.external_subject_id or "",
        first_write,
        idempotency_key=first.idempotency_key,
        access_reason="Preparing the patient's plan",
    )

    assert first.id == replay.id
    assert first.publication_number == 1
    assert first.entries[0].recipe_snapshot["name"].startswith("Plan soup")
    managed = session.scalars(
        sa.select(GroupMealPlan).where(GroupMealPlan.healthlabs_publication_entry_id.is_not(None))
    ).all()
    assert len(managed) == 1
    assert managed[0].healthlabs_recipe_revision_id == revision.id

    with pytest.raises(IdempotencyConflict):
        service.publish(
            target.external_subject_id or "",
            _write(revision, title="Changed"),
            idempotency_key=first.idempotency_key,
            access_reason="Preparing the patient's plan",
        )

    second = service.publish(
        target.external_subject_id or "",
        _write(revision, expected_publication_id=first.id),
        idempotency_key=f"publish-{uuid4()}",
        access_reason="Updating the patient's plan",
    )
    session.refresh(ordinary)

    assert second.publication_number == 2
    assert ordinary.healthlabs_publication_entry_id is None
    assert (
        session.scalar(sa.select(sa.func.count()).select_from(GroupMealPlan).where(GroupMealPlan.user_id == target.id))
        == 2
    )
    with pytest.raises(HealthLabsPlanConflict):
        service.publish(
            target.external_subject_id or "",
            _write(revision, expected_publication_id=first.id),
            idempotency_key=f"publish-{uuid4()}",
            access_reason="Stale editor",
        )


def test_publish_rejects_missing_inaccessible_and_unpinned_recipe_revisions(
    unique_user_fn_scoped: TestUser,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    editor = _editor(unique_user_fn_scoped, recipe_access=False)
    target = _target(healthlabs_provision, unfiltered_database)
    _, revision = _recipe_revision(unique_user_fn_scoped)
    service = PlanApiService(unfiltered_database.session, editor)

    with pytest.raises(HealthLabsPlanNotFound):
        service.publish(
            target.external_subject_id or "",
            _write(revision),
            idempotency_key=f"publish-{uuid4()}",
            access_reason="No recipe grant",
        )

    unique_user_fn_scoped.repos.session.add(
        HealthLabsCapabilityGrant(
            user_id=unique_user_fn_scoped.user_id,
            capability=HealthLabsCapability.RECIPES_READ_ALL.value,
            reason="Plan service test",
        )
    )
    unique_user_fn_scoped.repos.session.commit()
    service = PlanApiService(unfiltered_database.session, editor)
    missing = revision.__class__(
        id=uuid4(),
        recipe_id=revision.recipe_id,
        revision_number=2,
        snapshot={},
        content_sha256="b" * 64,
        actor_user_id=unique_user_fn_scoped.user_id,
    )
    with pytest.raises(HealthLabsPlanNotFound):
        service.publish(
            target.external_subject_id or "",
            _write(missing),
            idempotency_key=f"publish-{uuid4()}",
            access_reason="Missing revision",
        )


def test_current_plan_reads_pinned_snapshot_after_current_recipe_changes(
    unique_user_fn_scoped: TestUser,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    editor = _editor(unique_user_fn_scoped)
    target = _target(healthlabs_provision, unfiltered_database)
    recipe, revision = _recipe_revision(unique_user_fn_scoped, name="Original soup")
    service = PlanApiService(unfiltered_database.session, editor)
    published = service.publish(
        target.external_subject_id or "",
        _write(revision),
        idempotency_key=f"publish-{uuid4()}",
        access_reason="Publishing original recipe",
    )

    recipe.name = "Current recipe changed"
    unfiltered_database.session.commit()
    current = service.get_subject_current(
        target.external_subject_id or "",
        access_reason="Reviewing the patient's plan",
    )

    assert current is not None
    assert current.id == published.id
    assert current.entries[0].recipe_snapshot["name"].startswith("Original soup")


def test_publication_and_idempotency_result_roll_back_together_on_materialization_failure(
    unique_user_fn_scoped: TestUser,
    healthlabs_provision: Callable,
    unfiltered_database: AllRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = _editor(unique_user_fn_scoped)
    target = _target(healthlabs_provision, unfiltered_database)
    _, revision = _recipe_revision(unique_user_fn_scoped)
    session = unfiltered_database.session
    service = PlanApiService(session, editor)
    key = f"publish-{uuid4()}"
    publication_count = session.scalar(sa.select(sa.func.count()).select_from(HealthLabsPlanPublication))
    entry_count = session.scalar(sa.select(sa.func.count()).select_from(HealthLabsPlanPublicationEntry))

    def fail_materialization(*_args, **_kwargs) -> None:
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(service, "_materialize_current", fail_materialization)
    with pytest.raises(RuntimeError, match="materialization failed"):
        service.publish(
            target.external_subject_id or "",
            _write(revision),
            idempotency_key=key,
            access_reason="Testing transaction safety",
        )

    assert session.scalar(sa.select(sa.func.count()).select_from(HealthLabsPlanPublication)) == publication_count
    assert session.scalar(sa.select(sa.func.count()).select_from(HealthLabsPlanPublicationEntry)) == entry_count
    assert (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(HealthLabsIdempotencyOperation)
            .where(HealthLabsIdempotencyOperation.idempotency_key == key)
        )
        == 0
    )
