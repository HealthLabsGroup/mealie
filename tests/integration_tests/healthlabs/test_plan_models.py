from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mealie.db.models.healthlabs import (
    HealthLabsPlanEntryStatus,
    HealthLabsPlanPublication,
    HealthLabsPlanPublicationEntry,
)
from mealie.db.models.household.mealplan import GroupMealPlan
from mealie.db.models.recipe.recipe import HealthLabsRecipeRevision
from mealie.schema.recipe.recipe import Recipe
from tests.utils import TestUser


def _recipe_revision(user: TestUser) -> HealthLabsRecipeRevision:
    recipe = user.repos.recipes.create(
        Recipe(
            user_id=user.user_id,
            group_id=user.group_id,
            name=f"Plan recipe {uuid4()}",
        )
    )
    revision = HealthLabsRecipeRevision(
        recipe_id=recipe.id,
        revision_number=1,
        snapshot={"name": recipe.name},
        content_sha256="a" * 64,
        actor_user_id=user.user_id,
    )
    user.repos.session.add(revision)
    user.repos.session.flush()
    return revision


def _publication(
    session: Session,
    user: TestUser,
    *,
    subject: str,
    number: int = 1,
    superseded_at: datetime | None = None,
) -> HealthLabsPlanPublication:
    publication = HealthLabsPlanPublication(
        external_subject_id=subject,
        publication_number=number,
        content_sha256=f"{number:x}".rjust(64, "0"),
        from_date=date(2026, 10, 15),
        days=7,
        published_by_user_id=user.user_id,
        superseded_at=superseded_at,
        idempotency_key=f"publish-{subject}-{number}",
        request_sha256=f"{number:x}".rjust(64, "f"),
    )
    session.add(publication)
    session.flush()
    return publication


def test_publication_entries_pin_revisions_and_status_is_mutable(
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    revision = _recipe_revision(unique_user_fn_scoped)
    publication = _publication(
        session,
        unique_user_fn_scoped,
        subject=f"subject-{uuid4()}",
    )
    entry = HealthLabsPlanPublicationEntry(
        publication_id=publication.id,
        date=date(2026, 10, 15),
        entry_type="dinner",
        position=0,
        servings=2,
        recipe_id=revision.recipe_id,
        recipe_revision_id=revision.id,
        title_snapshot="Plan recipe",
    )
    session.add(entry)
    session.flush()
    status = HealthLabsPlanEntryStatus(
        publication_entry_id=entry.id,
        cooked_at=None,
        updated_by_user_id=unique_user_fn_scoped.user_id,
        version=1,
    )
    session.add(status)
    session.commit()

    status.cooked_at = datetime(2026, 10, 15, 18, 30, tzinfo=UTC)
    status.version = 2
    session.commit()

    assert entry.recipe_revision_id == revision.id
    assert status.version == 2
    assert status.cooked_at == datetime(2026, 10, 15, 18, 30, tzinfo=UTC)


def test_publication_number_and_current_publication_are_unique(
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    subject = f"subject-{uuid4()}"
    _publication(session, unique_user_fn_scoped, subject=subject)
    session.commit()

    with pytest.raises(IntegrityError):
        _publication(session, unique_user_fn_scoped, subject=subject, number=1)
    session.rollback()

    with pytest.raises(IntegrityError):
        _publication(session, unique_user_fn_scoped, subject=subject, number=2)
    session.rollback()


def test_publication_content_and_entries_are_immutable(
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    publication = _publication(
        session,
        unique_user_fn_scoped,
        subject=f"subject-{uuid4()}",
    )
    entry = HealthLabsPlanPublicationEntry(
        publication_id=publication.id,
        date=date(2026, 10, 15),
        entry_type="snack",
        position=0,
        title_snapshot="Fruit",
    )
    session.add(entry)
    session.commit()

    publication.superseded_at = datetime(2026, 10, 16, 8, tzinfo=UTC)
    session.commit()
    publication.content_sha256 = "b" * 64
    with pytest.raises(ValueError, match="publication content is immutable"):
        session.flush()
    session.rollback()

    entry = session.get(HealthLabsPlanPublicationEntry, entry.id)
    assert entry is not None
    entry.title_snapshot = "Changed fruit"
    with pytest.raises(ValueError, match="publication entries are immutable"):
        session.flush()
    session.rollback()


def test_entry_constraints_reject_unpinned_recipes_and_cross_publication_leftovers(
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    revision = _recipe_revision(unique_user_fn_scoped)
    first = _publication(
        session,
        unique_user_fn_scoped,
        subject=f"subject-{uuid4()}",
    )
    first_entry = HealthLabsPlanPublicationEntry(
        publication_id=first.id,
        date=date(2026, 10, 15),
        entry_type="dinner",
        position=0,
        recipe_id=revision.recipe_id,
        recipe_revision_id=revision.id,
        title_snapshot="Dinner",
    )
    session.add(first_entry)
    session.commit()

    second = _publication(
        session,
        unique_user_fn_scoped,
        subject=f"subject-{uuid4()}",
    )
    session.add(
        HealthLabsPlanPublicationEntry(
            publication_id=second.id,
            date=date(2026, 10, 16),
            entry_type="lunch",
            position=0,
            recipe_id=revision.recipe_id,
            recipe_revision_id=None,
            title_snapshot="Unpinned",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    second = _publication(
        session,
        unique_user_fn_scoped,
        subject=f"subject-{uuid4()}",
    )
    session.add(
        HealthLabsPlanPublicationEntry(
            publication_id=second.id,
            date=date(2026, 10, 16),
            entry_type="lunch",
            position=0,
            title_snapshot="Leftovers",
            leftover_of_entry_id=first_entry.id,
        )
    )
    with pytest.raises(ValueError, match="same publication"):
        session.commit()
    session.rollback()


def test_ordinary_native_meal_plan_keeps_healthlabs_links_null(
    unique_user_fn_scoped: TestUser,
) -> None:
    session = unique_user_fn_scoped.repos.session
    native = GroupMealPlan(
        session=session,
        date=date(2026, 10, 15),
        entry_type="dinner",
        title="Ordinary plan",
        text="",
        group_id=unique_user_fn_scoped.group_id,
        user_id=unique_user_fn_scoped.user_id,
    )
    session.add(native)
    session.commit()

    assert native.healthlabs_publication_entry_id is None
    assert native.healthlabs_recipe_revision_id is None
