"""add HealthLabs plan publications

Revision ID: 5b82f5da3b27
Revises: 4a71e4c92a16
Create Date: 2026-10-15 10:00:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "5b82f5da3b27"
down_revision: str | None = "4a71e4c92a16"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "healthlabs_plan_publications",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("external_subject_id", sa.String(length=255), nullable=False),
        sa.Column("publication_number", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("published_by_user_id", GUID(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("days IN (3, 7, 14)", name="healthlabs_plan_publication_days_valid"),
        sa.CheckConstraint(
            "publication_number > 0",
            name="healthlabs_plan_publication_number_positive",
        ),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "external_subject_id",
            "idempotency_key",
            name="healthlabs_plan_publication_idempotency_key",
        ),
        sa.UniqueConstraint(
            "external_subject_id",
            "publication_number",
            name="healthlabs_plan_publication_number_key",
        ),
    )
    op.create_index(
        "healthlabs_plan_current_subject_key",
        "healthlabs_plan_publications",
        ["external_subject_id"],
        unique=True,
        sqlite_where=sa.text("superseded_at IS NULL"),
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "ix_healthlabs_plan_publications_created_at",
        "healthlabs_plan_publications",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_plan_publications_external_subject_id",
        "healthlabs_plan_publications",
        ["external_subject_id"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_plan_publications_published_by_user_id",
        "healthlabs_plan_publications",
        ["published_by_user_id"],
        unique=False,
    )

    op.create_table(
        "healthlabs_plan_publication_entries",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("publication_id", GUID(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("servings", sa.Float(), nullable=True),
        sa.Column("recipe_id", GUID(), nullable=True),
        sa.Column("recipe_revision_id", GUID(), nullable=True),
        sa.Column("title_snapshot", sa.String(length=255), nullable=False),
        sa.Column("leftover_of_entry_id", GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "entry_type IN ('breakfast', 'lunch', 'dinner', 'snack')",
            name="healthlabs_plan_entry_type_valid",
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="healthlabs_plan_entry_position_nonnegative",
        ),
        sa.CheckConstraint(
            "(recipe_id IS NULL AND recipe_revision_id IS NULL) OR "
            "(recipe_id IS NOT NULL AND recipe_revision_id IS NOT NULL)",
            name="healthlabs_plan_entry_recipe_revision_pair",
        ),
        sa.CheckConstraint(
            "servings IS NULL OR servings > 0",
            name="healthlabs_plan_entry_servings_positive",
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
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["healthlabs_plan_publications.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["recipe_revision_id"],
            ["healthlabs_recipe_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
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
    )
    op.create_index(
        "ix_healthlabs_plan_publication_entries_created_at",
        "healthlabs_plan_publication_entries",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_plan_publication_entries_publication_id",
        "healthlabs_plan_publication_entries",
        ["publication_id"],
        unique=False,
    )
    op.create_index(
        "ix_healthlabs_plan_publication_entries_recipe_revision_id",
        "healthlabs_plan_publication_entries",
        ["recipe_revision_id"],
        unique=False,
    )

    op.create_table(
        "healthlabs_plan_entry_status",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("publication_entry_id", GUID(), nullable=False),
        sa.Column("cooked_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", GUID(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "version > 0",
            name="healthlabs_plan_entry_status_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["publication_entry_id"],
            ["healthlabs_plan_publication_entries.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "publication_entry_id",
            name="healthlabs_plan_entry_status_entry_key",
        ),
    )
    op.create_index(
        "ix_healthlabs_plan_entry_status_created_at",
        "healthlabs_plan_entry_status",
        ["created_at"],
        unique=False,
    )

    with op.batch_alter_table("group_meal_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("healthlabs_publication_entry_id", GUID(), nullable=True))
        batch_op.add_column(sa.Column("healthlabs_recipe_revision_id", GUID(), nullable=True))
        batch_op.create_foreign_key(
            "group_meal_plans_healthlabs_publication_entry_fk",
            "healthlabs_plan_publication_entries",
            ["healthlabs_publication_entry_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "group_meal_plans_healthlabs_recipe_revision_fk",
            "healthlabs_recipe_revisions",
            ["healthlabs_recipe_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_group_meal_plans_healthlabs_publication_entry_id",
            ["healthlabs_publication_entry_id"],
            unique=True,
        )
        batch_op.create_index(
            "ix_group_meal_plans_healthlabs_recipe_revision_id",
            ["healthlabs_recipe_revision_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("group_meal_plans", schema=None) as batch_op:
        batch_op.drop_index("ix_group_meal_plans_healthlabs_recipe_revision_id")
        batch_op.drop_index("ix_group_meal_plans_healthlabs_publication_entry_id")
        batch_op.drop_constraint(
            "group_meal_plans_healthlabs_recipe_revision_fk",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "group_meal_plans_healthlabs_publication_entry_fk",
            type_="foreignkey",
        )
        batch_op.drop_column("healthlabs_recipe_revision_id")
        batch_op.drop_column("healthlabs_publication_entry_id")

    op.drop_index(
        "ix_healthlabs_plan_entry_status_created_at",
        table_name="healthlabs_plan_entry_status",
    )
    op.drop_table("healthlabs_plan_entry_status")

    op.drop_index(
        "ix_healthlabs_plan_publication_entries_recipe_revision_id",
        table_name="healthlabs_plan_publication_entries",
    )
    op.drop_index(
        "ix_healthlabs_plan_publication_entries_publication_id",
        table_name="healthlabs_plan_publication_entries",
    )
    op.drop_index(
        "ix_healthlabs_plan_publication_entries_created_at",
        table_name="healthlabs_plan_publication_entries",
    )
    op.drop_table("healthlabs_plan_publication_entries")

    op.drop_index(
        "ix_healthlabs_plan_publications_published_by_user_id",
        table_name="healthlabs_plan_publications",
    )
    op.drop_index(
        "ix_healthlabs_plan_publications_external_subject_id",
        table_name="healthlabs_plan_publications",
    )
    op.drop_index(
        "ix_healthlabs_plan_publications_created_at",
        table_name="healthlabs_plan_publications",
    )
    op.drop_index(
        "healthlabs_plan_current_subject_key",
        table_name="healthlabs_plan_publications",
    )
    op.drop_table("healthlabs_plan_publications")
