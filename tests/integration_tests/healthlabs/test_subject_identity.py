from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mealie.db.models.users import User
from tests.utils import TestUser


def _new_managed_user(session: Session, template: User, subject: str) -> User:
    suffix = uuid4().hex
    return User(
        session=session,
        username=f"managed-{suffix}",
        full_name="Managed recipe user",
        email=f"managed-{suffix}@subjects.invalid",
        password="OIDC",
        admin=False,
        group=template.group.name,
        household=template.household.name,
        external_subject_id=subject,
        healthlabs_managed=True,
    )


def test_ordinary_upstream_user_has_no_subject_and_remains_unmanaged(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped.repos.session.get(User, unique_user_fn_scoped.user_id)

    assert user is not None
    assert user.external_subject_id is None
    assert user.healthlabs_managed is False


def test_subject_is_unique_when_present(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    template = session.get(User, unique_user_fn_scoped.user_id)
    assert template is not None
    subject = f"healthlabs-{uuid4()}"
    first = _new_managed_user(session, template, subject)
    session.add(first)
    session.commit()
    second = _new_managed_user(session, template, subject)
    session.add(second)

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_subject_cannot_change_after_assignment(unique_user_fn_scoped: TestUser) -> None:
    session = unique_user_fn_scoped.repos.session
    user = session.get(User, unique_user_fn_scoped.user_id)
    assert user is not None
    user.external_subject_id = f"healthlabs-{uuid4()}"
    user.healthlabs_managed = True
    session.commit()

    with pytest.raises(ValueError, match="external_subject_id is immutable"):
        user.external_subject_id = f"healthlabs-{uuid4()}"

    with pytest.raises(ValueError, match="external_subject_id is immutable"):
        user.external_subject_id = None
