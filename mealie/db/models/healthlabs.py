from __future__ import annotations

from datetime import date, datetime
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


class HealthLabsImportDraft(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_import_drafts"

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    actor_user_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(NaiveDateTime, nullable=False, index=True)


class HealthLabsPlanPublication(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_plan_publications"
    __table_args__ = (
        sa.UniqueConstraint(
            "external_subject_id",
            "publication_number",
            name="healthlabs_plan_publication_number_key",
        ),
        sa.UniqueConstraint(
            "external_subject_id",
            "idempotency_key",
            name="healthlabs_plan_publication_idempotency_key",
        ),
        sa.CheckConstraint(
            "publication_number > 0",
            name="healthlabs_plan_publication_number_positive",
        ),
        sa.CheckConstraint("days IN (3, 7, 14)", name="healthlabs_plan_publication_days_valid"),
        sa.Index(
            "healthlabs_plan_current_subject_key",
            "external_subject_id",
            unique=True,
            sqlite_where=sa.text("superseded_at IS NULL"),
            postgresql_where=sa.text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    external_subject_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    publication_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    from_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    days: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    published_by_user_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    published_at: Mapped[datetime] = mapped_column(
        NaiveDateTime,
        nullable=False,
        default=get_utc_now,
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        NaiveDateTime,
        nullable=True,
        active_history=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


@event.listens_for(HealthLabsPlanPublication, "before_update")
def prevent_plan_publication_content_mutation(_mapper: Any, _connection: Any, target: Any) -> None:
    state = sa.inspect(target)
    changed = {attribute.key for attribute in state.attrs if attribute.history.has_changes()}
    history = state.attrs.superseded_at.history
    superseding_once = (
        changed == {"superseded_at"}
        and target.superseded_at is not None
        and bool(history.deleted)
        and history.deleted[0] is None
    )
    if not superseding_once:
        raise ValueError("HealthLabs plan publication content is immutable")


@event.listens_for(HealthLabsPlanPublication, "before_delete")
def prevent_plan_publication_delete(*_args: Any) -> None:
    raise ValueError("HealthLabs plan publication content is immutable")


class HealthLabsPlanPublicationEntry(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_plan_publication_entries"
    __table_args__ = (
        sa.UniqueConstraint(
            "publication_id",
            "id",
            name="healthlabs_plan_publication_entry_identity_key",
        ),
        sa.UniqueConstraint(
            "publication_id",
            "date",
            "entry_type",
            "position",
            name="healthlabs_plan_publication_entry_position_key",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id", "leftover_of_entry_id"],
            [
                "healthlabs_plan_publication_entries.publication_id",
                "healthlabs_plan_publication_entries.id",
            ],
            name="healthlabs_plan_leftover_same_publication_fk",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "entry_type IN ('breakfast', 'lunch', 'dinner', 'snack')",
            name="healthlabs_plan_entry_type_valid",
        ),
        sa.CheckConstraint("position >= 0", name="healthlabs_plan_entry_position_nonnegative"),
        sa.CheckConstraint(
            "servings IS NULL OR servings > 0",
            name="healthlabs_plan_entry_servings_positive",
        ),
        sa.CheckConstraint(
            "(recipe_id IS NULL AND recipe_revision_id IS NULL) OR "
            "(recipe_id IS NOT NULL AND recipe_revision_id IS NOT NULL)",
            name="healthlabs_plan_entry_recipe_revision_pair",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    publication_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("healthlabs_plan_publications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    servings: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    recipe_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("recipes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    recipe_revision_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("healthlabs_recipe_revisions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    leftover_of_entry_id: Mapped[GUID | None] = mapped_column(GUID, nullable=True)


@event.listens_for(HealthLabsPlanPublicationEntry, "before_insert")
def validate_plan_leftover_publication(_mapper: Any, connection: Any, target: Any) -> None:
    if target.leftover_of_entry_id is None:
        return

    entry = HealthLabsPlanPublicationEntry.__table__
    publication_id = connection.scalar(
        sa.select(entry.c.publication_id).where(entry.c.id == target.leftover_of_entry_id)
    )
    if publication_id != target.publication_id:
        raise ValueError("HealthLabs leftover entry must belong to the same publication")


@event.listens_for(HealthLabsPlanPublicationEntry, "before_update")
@event.listens_for(HealthLabsPlanPublicationEntry, "before_delete")
def prevent_plan_publication_entry_mutation(*_args: Any) -> None:
    raise ValueError("HealthLabs plan publication entries are immutable")


class HealthLabsPlanEntryStatus(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_plan_entry_status"
    __table_args__ = (
        sa.UniqueConstraint(
            "publication_entry_id",
            name="healthlabs_plan_entry_status_entry_key",
        ),
        sa.CheckConstraint("version > 0", name="healthlabs_plan_entry_status_version_positive"),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    publication_entry_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("healthlabs_plan_publication_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    cooked_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, nullable=True)
    updated_by_user_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
