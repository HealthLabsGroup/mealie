"""add HealthLabs recipe identity

Revision ID: 4a71e4c92a16
Revises: 3697d65c5f92
Create Date: 2026-10-14 17:00:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "4a71e4c92a16"
down_revision: str | None = "3697d65c5f92"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("external_subject_id", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "healthlabs_managed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            "ix_users_external_subject_id",
            ["external_subject_id"],
            unique=True,
        )

    with op.batch_alter_table("recipes", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "healthlabs_scope",
                sa.String(length=16),
                nullable=False,
                server_default="personal",
            )
        )
        batch_op.add_column(
            sa.Column(
                "healthlabs_revision_number",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("copied_from_recipe_id", GUID(), nullable=True))
        batch_op.add_column(sa.Column("copied_from_revision_id", GUID(), nullable=True))
        batch_op.add_column(sa.Column("copied_at", sa.DateTime(), nullable=True))
        batch_op.create_index(
            "ix_recipes_copied_from_recipe_id",
            ["copied_from_recipe_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_recipes_copied_from_revision_id",
            ["copied_from_revision_id"],
            unique=False,
        )
        batch_op.create_check_constraint(
            "healthlabs_recipe_scope_valid",
            "healthlabs_scope IN ('personal', 'catalogue')",
        )
        batch_op.create_check_constraint(
            "healthlabs_recipe_revision_nonnegative",
            "healthlabs_revision_number >= 0",
        )

    op.create_table(
        "healthlabs_capability_grants",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("user_id", GUID(), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("granted_by_user_id", GUID(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_healthlabs_capability_grants_capability",
        "healthlabs_capability_grants",
        ["capability"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_capability_grants_user_id",
        "healthlabs_capability_grants",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "healthlabs_access_audit",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("actor_user_id", GUID(), nullable=True),
        sa.Column("subject_external_id", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_healthlabs_access_audit_action",
        "healthlabs_access_audit",
        ["action"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_access_audit_actor_user_id",
        "healthlabs_access_audit",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_access_audit_occurred_at",
        "healthlabs_access_audit",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_access_audit_subject_external_id",
        "healthlabs_access_audit",
        ["subject_external_id"],
        unique=False,
    )

    op.create_table(
        "healthlabs_recipe_revisions",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("recipe_id", GUID(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", GUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("revision_number > 0", name="healthlabs_recipe_revision_positive"),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recipe_id",
            "revision_number",
            name="healthlabs_recipe_revision_number_key",
        ),
    )
    op.create_index(
        "ix_healthlabs_recipe_revisions_actor_user_id",
        "healthlabs_recipe_revisions",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_recipe_revisions_content_sha256",
        "healthlabs_recipe_revisions",
        ["content_sha256"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_recipe_revisions_created_at",
        "healthlabs_recipe_revisions",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_recipe_revisions_recipe_id",
        "healthlabs_recipe_revisions",
        ["recipe_id"],
        unique=False,
    )

    op.create_table(
        "healthlabs_idempotency_operations",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("actor_user_id", GUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'completed')",
            name="healthlabs_idempotency_state_valid",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="healthlabs_idempotency_actor_key",
        ),
    )
    op.create_index(
        "ix_healthlabs_idempotency_operations_actor_user_id",
        "healthlabs_idempotency_operations",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_idempotency_operations_expires_at",
        "healthlabs_idempotency_operations",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_healthlabs_idempotency_operations_expires_at",
        table_name="healthlabs_idempotency_operations",
    )
    op.drop_index(
        "ix_healthlabs_idempotency_operations_actor_user_id",
        table_name="healthlabs_idempotency_operations",
    )
    op.drop_table("healthlabs_idempotency_operations")

    op.drop_index(
        "ix_healthlabs_recipe_revisions_recipe_id",
        table_name="healthlabs_recipe_revisions",
    )
    op.drop_index(
        "ix_healthlabs_recipe_revisions_created_at",
        table_name="healthlabs_recipe_revisions",
    )
    op.drop_index(
        "ix_healthlabs_recipe_revisions_content_sha256",
        table_name="healthlabs_recipe_revisions",
    )
    op.drop_index(
        "ix_healthlabs_recipe_revisions_actor_user_id",
        table_name="healthlabs_recipe_revisions",
    )
    op.drop_table("healthlabs_recipe_revisions")

    op.drop_index(
        "ix_healthlabs_access_audit_subject_external_id",
        table_name="healthlabs_access_audit",
    )
    op.drop_index("ix_healthlabs_access_audit_occurred_at", table_name="healthlabs_access_audit")
    op.drop_index("ix_healthlabs_access_audit_actor_user_id", table_name="healthlabs_access_audit")
    op.drop_index("ix_healthlabs_access_audit_action", table_name="healthlabs_access_audit")
    op.drop_table("healthlabs_access_audit")

    op.drop_index(
        "ix_healthlabs_capability_grants_user_id",
        table_name="healthlabs_capability_grants",
    )
    op.drop_index(
        "ix_healthlabs_capability_grants_capability",
        table_name="healthlabs_capability_grants",
    )
    op.drop_table("healthlabs_capability_grants")

    with op.batch_alter_table("recipes", schema=None) as batch_op:
        batch_op.drop_constraint("healthlabs_recipe_revision_nonnegative", type_="check")
        batch_op.drop_constraint("healthlabs_recipe_scope_valid", type_="check")
        batch_op.drop_index("ix_recipes_copied_from_revision_id")
        batch_op.drop_index("ix_recipes_copied_from_recipe_id")
        batch_op.drop_column("copied_at")
        batch_op.drop_column("copied_from_revision_id")
        batch_op.drop_column("copied_from_recipe_id")
        batch_op.drop_column("healthlabs_revision_number")
        batch_op.drop_column("healthlabs_scope")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_external_subject_id")
        batch_op.drop_column("healthlabs_managed")
        batch_op.drop_column("external_subject_id")
