"""add HealthLabs nutrition curator

Revision ID: c146e8a30f9d
Revises: b035d7f29e8c
Create Date: 2026-10-18 13:00:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "c146e8a30f9d"
down_revision: str | None = "b035d7f29e8c"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def _base_columns() -> tuple[sa.Column, sa.Column, sa.Column]:
    return (
        sa.Column("id", GUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
    )


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("user_type", sa.String(length=16), server_default="human", nullable=False))
        batch_op.create_check_constraint("users_user_type_valid", "user_type IN ('human', 'agent')")

    op.create_table(
        "healthlabs_nutrition_curator_decisions",
        *_base_columns(),
        sa.Column("work_id", GUID(), nullable=True),
        sa.Column("group_id", GUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("work_input_sha256", sa.String(length=64), nullable=False),
        sa.Column("curator_sha256", sa.String(length=64), nullable=False),
        sa.Column("agent_user_id", GUID(), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("candidate_snapshot", sa.JSON(), nullable=False),
        sa.Column("selected_ids", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("runner_up_confidence", sa.Float(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("rationale", sa.String(length=280), nullable=True),
        sa.Column("provider_id", GUID(), nullable=True),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("provider_model", sa.String(length=255), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
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
        sa.ForeignKeyConstraint(["work_id"], ["healthlabs_nutrition_work.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["provider_id"], ["ai_providers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind",
            "target_id",
            "curator_sha256",
            name="healthlabs_nutrition_curator_target_fingerprint_key",
        ),
    )
    op.create_index(
        "healthlabs_nutrition_curator_due_idx",
        "healthlabs_nutrition_curator_decisions",
        ["state", "next_attempt_at", "lease_expires_at"],
    )
    for column in ("created_at", "work_id", "group_id", "agent_user_id"):
        op.create_index(
            f"ix_healthlabs_nutrition_curator_decisions_{column}",
            "healthlabs_nutrition_curator_decisions",
            [column],
        )

    op.create_table(
        "healthlabs_food_portion_mappings",
        *_base_columns(),
        sa.Column("group_id", GUID(), nullable=False),
        sa.Column("ingredient_food_id", GUID(), nullable=False),
        sa.Column("ingredient_unit_id", GUID(), nullable=False),
        sa.Column("food_portion_id", GUID(), nullable=False),
        sa.Column("reviewed_by_user_id", GUID(), nullable=False),
        sa.Column("curator_decision_id", GUID(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ingredient_food_id"], ["ingredient_foods.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ingredient_unit_id"], ["ingredient_units.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["food_portion_id"], ["healthlabs_food_portions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["curator_decision_id"],
            ["healthlabs_nutrition_curator_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "ingredient_food_id",
            "ingredient_unit_id",
            name="healthlabs_food_portion_mapping_group_food_unit_key",
        ),
    )
    for column in (
        "created_at",
        "group_id",
        "ingredient_food_id",
        "ingredient_unit_id",
        "food_portion_id",
        "reviewed_by_user_id",
        "curator_decision_id",
    ):
        op.create_index(
            f"ix_healthlabs_food_portion_mappings_{column}",
            "healthlabs_food_portion_mappings",
            [column],
        )


def downgrade() -> None:
    connection = op.get_bind()
    decisions = sa.table("healthlabs_nutrition_curator_decisions", sa.column("id", GUID()))
    mappings = sa.table("healthlabs_food_portion_mappings", sa.column("id", GUID()))
    if connection.scalar(sa.select(sa.func.count()).select_from(decisions)) or connection.scalar(
        sa.select(sa.func.count()).select_from(mappings)
    ):
        raise RuntimeError("Cannot downgrade while Curator evidence still exists; export or remove it first")

    users = sa.table("users", sa.column("user_type", sa.String()))
    if connection.scalar(sa.select(sa.func.count()).select_from(users).where(users.c.user_type == "agent")):
        raise RuntimeError("Cannot downgrade while agent users still exist; remove them first")

    op.drop_table("healthlabs_food_portion_mappings")
    op.drop_table("healthlabs_nutrition_curator_decisions")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("users_user_type_valid", type_="check")
        batch_op.drop_column("user_type")
