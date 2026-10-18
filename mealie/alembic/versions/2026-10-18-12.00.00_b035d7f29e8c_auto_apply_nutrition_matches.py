"""auto apply nutrition matches

Revision ID: b035d7f29e8c
Revises: af24c6e18d7b
Create Date: 2026-10-18 12:00:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "b035d7f29e8c"
down_revision: str | None = "af24c6e18d7b"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("healthlabs_food_matches") as batch_op:
        batch_op.add_column(sa.Column("auto_applied", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table(
        "healthlabs_food_match_suppressions",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.Column("ingredient_food_id", GUID(), nullable=False),
        sa.Column("suppressed_by_user_id", GUID(), nullable=True),
        sa.Column("suppressed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["ingredient_food_id"], ["ingredient_foods.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["suppressed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ingredient_food_id", name="healthlabs_food_match_suppression_ingredient_key"),
    )
    for column in ("created_at", "ingredient_food_id", "suppressed_by_user_id"):
        op.create_index(
            f"ix_healthlabs_food_match_suppressions_{column}",
            "healthlabs_food_match_suppressions",
            [column],
        )


def downgrade() -> None:
    op.drop_table("healthlabs_food_match_suppressions")
    with op.batch_alter_table("healthlabs_food_matches") as batch_op:
        batch_op.drop_column("auto_applied")
