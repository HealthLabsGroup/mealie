from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, String, event
from sqlalchemy.orm import Mapped, mapped_column

from mealie.db.models._model_base import BaseMixins, SqlAlchemyBase
from mealie.db.models._model_utils.datetime import NaiveDateTime, get_utc_now
from mealie.db.models._model_utils.guid import GUID


class HealthLabsCapabilityGrant(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_capability_grants"

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    user_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    granted_by_user_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        NaiveDateTime,
        nullable=False,
        default=get_utc_now,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, nullable=True)


class HealthLabsAccessAudit(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_access_audit"

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    actor_user_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subject_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        NaiveDateTime,
        nullable=False,
        default=get_utc_now,
        index=True,
    )


@event.listens_for(HealthLabsAccessAudit, "before_update")
@event.listens_for(HealthLabsAccessAudit, "before_delete")
def prevent_healthlabs_audit_mutation(*_args: Any) -> None:
    raise ValueError("HealthLabs access audit events are immutable")


class HealthLabsIdempotencyOperation(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_idempotency_operations"
    __table_args__ = (
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="healthlabs_idempotency_actor_key",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'completed')",
            name="healthlabs_idempotency_state_valid",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    actor_user_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    response_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(NaiveDateTime, nullable=False, index=True)
