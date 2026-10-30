"""Persistent bounded metadata scanning and request usage.

Revision ID: a61f09c73d22
Revises: f59a3d8ba2e4
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID

revision = "a61f09c73d22"
down_revision = "f59a3d8ba2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "healthlabs_recipe_metadata_scans",
        sa.Column("id", GUID(), primary_key=True, nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False, unique=True),
        sa.Column("cursor_recipe_id", GUID(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cycles", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("update_at", sa.DateTime(), nullable=True),
    )
    op.add_column("healthlabs_recipe_metadata_estimates", sa.Column("usage", sa.JSON(), nullable=True))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM healthlabs_recipe_metadata_estimates WHERE usage IS NOT NULL")
    ):
        raise RuntimeError("Metadata request usage exists; archive it before downgrade")
    op.drop_table("healthlabs_recipe_metadata_scans")
    op.drop_column("healthlabs_recipe_metadata_estimates", "usage")
