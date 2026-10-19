from __future__ import annotations

import secrets
from contextlib import nullcontext
from threading import Lock
from uuid import UUID

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from mealie.db.models.group import Group
from mealie.db.models.healthlabs import (
    HealthLabsCapabilityGrant,
    HealthLabsFoodPortionMapping,
    HealthLabsNutritionCuratorDecision,
)
from mealie.db.models.household import Household
from mealie.db.models.users import LongLiveToken, PasswordResetModel, User, UserType
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.user import PrivateUser

CURATOR_SUBJECT_PREFIX = "healthlabs:agent:nutrition-curator:"
_SQLITE_CURATOR_IDENTITY_LOCK = Lock()


def assert_interactive_user(user: PrivateUser | User) -> None:
    if user.user_type is UserType.AGENT or (
        user.healthlabs_managed and (user.external_subject_id or "").startswith(CURATOR_SUBJECT_PREFIX)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent users cannot use interactive account operations",
        )


def delete_group_curator_identities(session: Session, group_id: UUID) -> None:
    agent_ids = session.scalars(
        sa.select(User.id).where(User.group_id == group_id, User.user_type == UserType.AGENT)
    ).all()
    if not agent_ids:
        return

    session.execute(sa.delete(HealthLabsFoodPortionMapping).where(HealthLabsFoodPortionMapping.group_id == group_id))
    session.execute(
        sa.delete(HealthLabsNutritionCuratorDecision).where(HealthLabsNutritionCuratorDecision.group_id == group_id)
    )
    session.execute(sa.delete(LongLiveToken).where(LongLiveToken.user_id.in_(agent_ids)))
    session.execute(sa.delete(PasswordResetModel).where(PasswordResetModel.user_id.in_(agent_ids)))
    session.execute(sa.delete(User).where(User.id.in_(agent_ids)))


class NutritionCuratorIdentityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_or_create(self, group_id: UUID) -> PrivateUser:
        process_lock = (
            _SQLITE_CURATOR_IDENTITY_LOCK if self.session.get_bind().dialect.name == "sqlite" else nullcontext()
        )
        with process_lock:
            subject = f"{CURATOR_SUBJECT_PREFIX}{group_id}"
            self._acquire_lock(subject)
            user = self.session.scalar(sa.select(User).where(User.external_subject_id == subject))
            if user is None:
                user = self._create(group_id, subject)
            result = PrivateUser.model_validate(user)
            self.session.commit()
            return result

    def _create(self, group_id: UUID, subject: str) -> User:
        group = self.session.get(Group, group_id)
        if group is None:
            raise LookupError(f"Group {group_id} does not exist")

        household = self.session.scalar(
            sa.select(Household)
            .where(Household.group_id == group_id)
            .order_by(Household.created_at, Household.id)
            .limit(1)
        )
        if household is None:
            raise RuntimeError(f"Group {group_id} has no household")

        from mealie.core.security import hash_password

        group_hex = group_id.hex
        user = User(
            session=self.session,
            username=f"nutrition-curator-{group_hex}",
            full_name="Apple — Nutrition Curator",
            email=f"{group_hex}@nutrition-curator.invalid",
            password=hash_password(secrets.token_urlsafe(48)),
            admin=False,
            advanced=False,
            can_manage_household=False,
            can_manage=False,
            can_invite=False,
            can_organize=False,
            group=group.name,
            household=household.name,
            user_type=UserType.AGENT,
            external_subject_id=subject,
            healthlabs_managed=True,
        )
        self.session.add(user)
        self.session.flush()
        self.session.add(
            HealthLabsCapabilityGrant(
                user_id=user.id,
                capability=HealthLabsCapability.NUTRITION_WRITE.value,
                reason="Apple nutrition curator identity provisioning",
            )
        )
        return user

    def _acquire_lock(self, subject: str) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(:subject))"),
                {"subject": subject},
            )
        elif self.session.get_bind().dialect.name == "sqlite":
            self.session.execute(sa.text("BEGIN IMMEDIATE"))
