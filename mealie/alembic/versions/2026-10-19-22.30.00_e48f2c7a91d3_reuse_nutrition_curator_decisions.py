"""reuse HealthLabs nutrition curator decisions

Revision ID: e48f2c7a91d3
Revises: d257f9c4a6b8
Create Date: 2026-10-19 22:30:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "e48f2c7a91d3"
down_revision: str | None = "d257f9c4a6b8"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("healthlabs_nutrition_curator_decisions") as batch_op:
        # Legacy decisions stay null and cannot seed reuse because their exact reusable context was not persisted.
        batch_op.add_column(sa.Column("reuse_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("reused_from_decision_id", GUID(), nullable=True))
        batch_op.create_foreign_key(
            "fk_hl_nutrition_curator_reused_from",
            "healthlabs_nutrition_curator_decisions",
            ["reused_from_decision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "healthlabs_nutrition_curator_reuse_idx",
        "healthlabs_nutrition_curator_decisions",
        ["group_id", "kind", "state", "reuse_sha256", "reused_from_decision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "healthlabs_nutrition_curator_reuse_idx",
        table_name="healthlabs_nutrition_curator_decisions",
    )
    with op.batch_alter_table("healthlabs_nutrition_curator_decisions") as batch_op:
        batch_op.drop_constraint("fk_hl_nutrition_curator_reused_from", type_="foreignkey")
        batch_op.drop_column("reused_from_decision_id")
        batch_op.drop_column("reuse_sha256")
