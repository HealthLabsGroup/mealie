"""Isolated recipe metadata evidence and durable refresh work.

Revision ID: f59a3d8ba2e4
Revises: e48f2c7a91d3
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID

revision = "f59a3d8ba2e4"
down_revision = "e48f2c7a91d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "healthlabs_recipe_metadata_estimates",
        sa.Column("id", GUID(), primary_key=True, nullable=False),
        sa.Column("group_id", GUID(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipe_id", GUID(), sa.ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("authoring_fingerprint", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("provider_name", sa.String(64), nullable=True),
        sa.Column("provider_model", sa.String(255), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("lease_token", GUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "group_id", "recipe_id", "input_fingerprint", "policy_version", name="hl_recipe_metadata_identity_key"
        ),
        sa.CheckConstraint(
            "state IN ('pending','running','ready','rejected','failed','superseded')",
            name="hl_recipe_metadata_state_valid",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="hl_recipe_metadata_attempt_valid"),
    )
    op.create_index(
        "hl_recipe_metadata_recipe_idx", "healthlabs_recipe_metadata_estimates", ["group_id", "recipe_id", "created_at"]
    )
    op.create_index(
        "hl_recipe_metadata_due_idx",
        "healthlabs_recipe_metadata_estimates",
        ["state", "next_attempt_at", "lease_expires_at"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM healthlabs_recipe_metadata_estimates")):
        raise RuntimeError("Recipe metadata evidence still exists; archive it before downgrade")
    op.drop_table("healthlabs_recipe_metadata_estimates")
