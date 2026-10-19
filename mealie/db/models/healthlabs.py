from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, String, event
from sqlalchemy.orm import Mapped, mapped_column, validates

from mealie.db.models._model_base import BaseMixins, SqlAlchemyBase
from mealie.db.models._model_utils.datetime import NaiveDateTime, get_utc_now
from mealie.db.models._model_utils.guid import GUID
from mealie.db.models.recipe.recipe import RecipeModel


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


class HealthLabsNutrientSourceRelease(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_nutrient_source_releases"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider",
            "version",
            "country",
            name="healthlabs_nutrient_source_identity_key",
        ),
        sa.UniqueConstraint(
            "provider",
            "sha256",
            name="healthlabs_nutrient_source_checksum_key",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    country: Mapped[str] = mapped_column(String(5), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    license_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    attribution: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        NaiveDateTime,
        nullable=False,
        default=get_utc_now,
    )


class HealthLabsNutrientFood(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_nutrient_foods"
    __table_args__ = (
        sa.UniqueConstraint(
            "source_release_id",
            "source_food_id",
            name="healthlabs_nutrient_food_source_key",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    source_release_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("healthlabs_nutrient_source_releases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_food_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    preparation_state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nutrient_values: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)


class HealthLabsFoodDensity(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_food_densities"
    __table_args__ = (
        sa.UniqueConstraint(
            "source_release_id",
            "source_record_id",
            name="healthlabs_food_density_source_record_key",
        ),
        sa.CheckConstraint("grams_per_ml > 0", name="healthlabs_food_density_positive"),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    source_release_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("healthlabs_nutrient_source_releases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    grams_per_ml: Mapped[float] = mapped_column(sa.Float, nullable=False)
    reference_id: Mapped[str] = mapped_column(String(100), nullable=False)


class HealthLabsFoodMatch(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_food_matches"
    __table_args__ = (
        sa.UniqueConstraint(
            "ingredient_food_id",
            name="healthlabs_food_match_ingredient_key",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="healthlabs_food_match_confidence_valid",
        ),
        sa.CheckConstraint(
            "(reviewed_by_user_id IS NULL AND reviewed_at IS NULL) OR "
            "(reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="healthlabs_food_match_review_pair_valid",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    ingredient_food_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("ingredient_foods.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    nutrient_food_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("healthlabs_nutrient_foods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    country: Mapped[str] = mapped_column(String(5), nullable=False)
    match_method: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    auto_applied: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    food_group: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plant_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    allergens: Mapped[dict[str, str] | None] = mapped_column(sa.JSON, nullable=True)
    diets: Mapped[dict[str, str] | None] = mapped_column(sa.JSON, nullable=True)
    reviewed_by_user_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, nullable=True)


class HealthLabsFoodMatchSuppression(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_food_match_suppressions"

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    ingredient_food_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("ingredient_foods.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    suppressed_by_user_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    suppressed_at: Mapped[datetime] = mapped_column(NaiveDateTime, nullable=False, default=get_utc_now)


class HealthLabsFoodPortion(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_food_portions"
    __table_args__ = (
        sa.Index(
            "healthlabs_food_portion_source_key",
            "nutrient_food_id",
            "description",
            "amount",
            "unit",
            unique=True,
            sqlite_where=sa.text("source_managed = 1"),
            postgresql_where=sa.text("source_managed IS TRUE"),
        ),
        sa.Index(
            "healthlabs_food_portion_group_key",
            "group_id",
            "nutrient_food_id",
            "description",
            "amount",
            "unit",
            unique=True,
            sqlite_where=sa.text("source_managed = 0"),
            postgresql_where=sa.text("source_managed IS FALSE"),
        ),
        sa.CheckConstraint("amount > 0", name="healthlabs_food_portion_amount_positive"),
        sa.CheckConstraint("grams > 0", name="healthlabs_food_portion_grams_positive"),
        sa.CheckConstraint(
            "(source_managed AND group_id IS NULL) OR (NOT source_managed AND group_id IS NOT NULL)",
            name="healthlabs_food_portion_owner_valid",
        ),
        sa.CheckConstraint(
            "density_id IS NULL OR source_managed",
            name="healthlabs_food_portion_density_source_valid",
        ),
        sa.UniqueConstraint(
            "nutrient_food_id",
            "density_id",
            name="healthlabs_food_portion_density_key",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    nutrient_food_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("healthlabs_nutrient_foods.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    density_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey(
            "healthlabs_food_densities.id",
            name="fk_hl_food_portion_density",
            ondelete="RESTRICT",
        ),
        nullable=True,
        index=True,
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[float] = mapped_column(sa.Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    grams: Mapped[float] = mapped_column(sa.Float, nullable=False)
    source_managed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True, server_default=sa.true())


class HealthLabsNutritionWork(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_nutrition_work"
    __table_args__ = (
        sa.UniqueConstraint("kind", "target_id", name="healthlabs_nutrition_work_target_key"),
        sa.CheckConstraint(
            "kind IN ('ingredient_structure', 'food_match', 'portion_resolution')",
            name="healthlabs_nutrition_work_kind_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'review_required', 'completed', 'failed')",
            name="healthlabs_nutrition_work_state_valid",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="healthlabs_nutrition_work_attempt_nonnegative"),
        sa.Index(
            "healthlabs_nutrition_work_due_idx",
            "state",
            "next_attempt_at",
            "lease_expires_at",
        ),
        sa.Index("healthlabs_nutrition_work_scan_idx", "kind", "update_at"),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    group_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # shortcut: polymorphic nutrition targets cannot use one database FK; discovery removes stale rows.
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    attempt_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(NaiveDateTime, nullable=False, default=get_utc_now)
    lease_expires_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, nullable=True)
    provider_requested_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)


class HealthLabsNutritionCuratorDecision(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_nutrition_curator_decisions"
    __table_args__ = (
        sa.UniqueConstraint(
            "kind",
            "target_id",
            "curator_sha256",
            name="healthlabs_nutrition_curator_target_fingerprint_key",
        ),
        sa.CheckConstraint(
            "kind IN ('ingredient_structure', 'food_match', 'portion_resolution')",
            name="healthlabs_nutrition_curator_kind_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'approved', 'insufficient_evidence', 'failed')",
            name="healthlabs_nutrition_curator_state_valid",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="healthlabs_nutrition_curator_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="healthlabs_nutrition_curator_confidence_valid",
        ),
        sa.CheckConstraint(
            "runner_up_confidence IS NULL OR (runner_up_confidence >= 0 AND runner_up_confidence <= 1)",
            name="healthlabs_nutrition_curator_runner_up_confidence_valid",
        ),
        sa.Index(
            "healthlabs_nutrition_curator_due_idx",
            "state",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    work_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("healthlabs_nutrition_work.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    group_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    work_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    curator_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_user_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    state: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending",
        server_default="pending",
        active_history=True,
    )
    attempt_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(NaiveDateTime, nullable=False, default=get_utc_now)
    lease_expires_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, nullable=True)
    candidate_snapshot: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    selected_ids: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    runner_up_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rationale: Mapped[str | None] = mapped_column(String(280), nullable=True)
    provider_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("ai_providers.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    requested_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)

    @validates("confidence", "runner_up_confidence")
    def validate_score(self, key: str, value: float | None) -> float | None:
        if value is not None and (not isfinite(value) or not 0 <= value <= 1):
            raise ValueError(f"{key} must be finite and between 0 and 1")
        return value


_TERMINAL_CURATOR_STATES = frozenset({"approved", "insufficient_evidence", "failed"})


@event.listens_for(HealthLabsNutritionCuratorDecision, "before_update")
def prevent_terminal_curator_decision_update(_mapper: Any, _connection: Any, target: Any) -> None:
    history = sa.inspect(target).attrs.state.history
    previous_state = history.deleted[0] if history.deleted else target.state
    if previous_state in _TERMINAL_CURATOR_STATES:
        raise ValueError("terminal Curator decisions are immutable")


@event.listens_for(HealthLabsNutritionCuratorDecision, "before_delete")
def prevent_terminal_curator_decision_delete(_mapper: Any, _connection: Any, target: Any) -> None:
    if target.state in _TERMINAL_CURATOR_STATES:
        raise ValueError("terminal Curator decisions are immutable")


class HealthLabsFoodPortionMapping(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_food_portion_mappings"
    __table_args__ = (
        sa.UniqueConstraint(
            "group_id",
            "ingredient_food_id",
            "ingredient_unit_id",
            name="healthlabs_food_portion_mapping_group_food_unit_key",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    group_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ingredient_food_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("ingredient_foods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    ingredient_unit_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("ingredient_units.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    food_portion_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("healthlabs_food_portions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reviewed_by_user_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    curator_decision_id: Mapped[GUID] = mapped_column(
        GUID,
        ForeignKey("healthlabs_nutrition_curator_decisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class HealthLabsNutritionProviderUsage(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_nutrition_provider_usage"
    __table_args__ = (
        sa.UniqueConstraint("usage_date", name="healthlabs_nutrition_provider_usage_date_key"),
        sa.CheckConstraint(
            "request_count >= 0",
            name="healthlabs_nutrition_provider_usage_count_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_microusd >= 0",
            name="healthlabs_nutrition_provider_usage_reserved_nonnegative",
        ),
        sa.CheckConstraint(
            "spent_microusd >= 0",
            name="healthlabs_nutrition_provider_usage_spent_nonnegative",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND cached_input_tokens >= 0 AND output_tokens >= 0",
            name="healthlabs_nutrition_provider_usage_tokens_nonnegative",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    usage_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    request_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    reserved_microusd: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    spent_microusd: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    cached_input_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")


class HealthLabsBrandedProduct(SqlAlchemyBase, BaseMixins):
    __tablename__ = "healthlabs_branded_products"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider",
            "barcode",
            name="healthlabs_branded_product_provider_barcode_key",
        ),
    )

    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=GUID.generate)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    barcode: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(500), nullable=False)
    source_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    license_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    nutrient_values: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)


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
        ForeignKey("recipes.id", ondelete="SET NULL"),
        nullable=True,
    )
    recipe_revision_id: Mapped[GUID | None] = mapped_column(
        GUID,
        ForeignKey("healthlabs_recipe_revisions.id", ondelete="SET NULL"),
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


@event.listens_for(RecipeModel, "before_delete")
def detach_deleted_recipe_from_plan_entries(_mapper: Any, connection: Any, target: RecipeModel) -> None:
    # SQLite may not enforce FK actions, so preserve the same historical-plan state explicitly.
    entry = HealthLabsPlanPublicationEntry.__table__
    connection.execute(
        sa.update(entry).where(entry.c.recipe_id == target.id).values(recipe_id=None, recipe_revision_id=None)
    )


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
