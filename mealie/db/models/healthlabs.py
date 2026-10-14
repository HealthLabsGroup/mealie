from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
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
