"""add HealthLabs nutrition work

Revision ID: 9e13f4a6b8c0
Revises: 8d02e1f3a5b7
Create Date: 2026-10-17 21:00:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "9e13f4a6b8c0"
down_revision: str | None = "8d02e1f3a5b7"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def _base_columns() -> tuple[sa.Column, sa.Column, sa.Column]:
    return (
        sa.Column("id", GUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
    )


def upgrade() -> None:
    op.create_table(
        "healthlabs_nutrition_work",
        *_base_columns(),
        sa.Column("group_id", GUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("provider_requested_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "kind IN ('ingredient_structure', 'food_match', 'portion_resolution')",
            name="healthlabs_nutrition_work_kind_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'review_required', 'completed', 'failed')",
            name="healthlabs_nutrition_work_state_valid",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="healthlabs_nutrition_work_attempt_nonnegative"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "target_id", name="healthlabs_nutrition_work_target_key"),
    )
    op.create_index(
        "healthlabs_nutrition_work_due_idx",
        "healthlabs_nutrition_work",
        ["state", "next_attempt_at", "lease_expires_at"],
    )
    op.create_index(
        "healthlabs_nutrition_work_scan_idx",
        "healthlabs_nutrition_work",
        ["kind", "update_at"],
    )
    op.create_index(
        "ix_healthlabs_nutrition_work_created_at",
        "healthlabs_nutrition_work",
        ["created_at"],
    )
    op.create_index(
        "ix_healthlabs_nutrition_work_group_id",
        "healthlabs_nutrition_work",
        ["group_id"],
    )

    op.create_table(
        "healthlabs_nutrition_provider_usage",
        *_base_columns(),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "request_count >= 0",
            name="healthlabs_nutrition_provider_usage_count_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("usage_date", name="healthlabs_nutrition_provider_usage_date_key"),
    )
    op.create_index(
        "ix_healthlabs_nutrition_provider_usage_created_at",
        "healthlabs_nutrition_provider_usage",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_healthlabs_nutrition_provider_usage_created_at",
        table_name="healthlabs_nutrition_provider_usage",
    )
    op.drop_table("healthlabs_nutrition_provider_usage")
    op.drop_index("ix_healthlabs_nutrition_work_group_id", table_name="healthlabs_nutrition_work")
    op.drop_index("ix_healthlabs_nutrition_work_created_at", table_name="healthlabs_nutrition_work")
    op.drop_index("healthlabs_nutrition_work_scan_idx", table_name="healthlabs_nutrition_work")
    op.drop_index("healthlabs_nutrition_work_due_idx", table_name="healthlabs_nutrition_work")
    op.drop_table("healthlabs_nutrition_work")
