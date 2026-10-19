"""add HealthLabs food densities

Revision ID: d257f9c4a6b8
Revises: c146e8a30f9d
Create Date: 2026-10-19 14:00:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "d257f9c4a6b8"
down_revision: str | None = "c146e8a30f9d"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "healthlabs_food_densities",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("update_at", sa.DateTime(), nullable=True),
        sa.Column("source_release_id", GUID(), nullable=False),
        sa.Column("source_record_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("name_normalized", sa.String(length=255), nullable=False),
        sa.Column("grams_per_ml", sa.Float(), nullable=False),
        sa.Column("reference_id", sa.String(length=100), nullable=False),
        sa.CheckConstraint("grams_per_ml > 0", name="healthlabs_food_density_positive"),
        sa.ForeignKeyConstraint(
            ["source_release_id"],
            ["healthlabs_nutrient_source_releases.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_release_id",
            "source_record_id",
            name="healthlabs_food_density_source_record_key",
        ),
    )
    for column in ("created_at", "source_release_id", "name_normalized"):
        op.create_index(
            f"ix_healthlabs_food_densities_{column}",
            "healthlabs_food_densities",
            [column],
        )

    with op.batch_alter_table("healthlabs_food_portions") as batch_op:
        batch_op.add_column(sa.Column("density_id", GUID(), nullable=True))
        batch_op.create_foreign_key(
            "fk_healthlabs_food_portions_density_id_healthlabs_food_densities",
            "healthlabs_food_densities",
            ["density_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "healthlabs_food_portion_density_source_valid",
            "density_id IS NULL OR source_managed",
        )
        batch_op.create_unique_constraint(
            "healthlabs_food_portion_density_key",
            ["nutrient_food_id", "density_id"],
        )
    op.create_index(
        "ix_healthlabs_food_portions_density_id",
        "healthlabs_food_portions",
        ["density_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_healthlabs_food_portions_density_id", table_name="healthlabs_food_portions")
    with op.batch_alter_table("healthlabs_food_portions") as batch_op:
        batch_op.drop_constraint("healthlabs_food_portion_density_key", type_="unique")
        batch_op.drop_constraint("healthlabs_food_portion_density_source_valid", type_="check")
        batch_op.drop_constraint(
            "fk_healthlabs_food_portions_density_id_healthlabs_food_densities",
            type_="foreignkey",
        )
        batch_op.drop_column("density_id")

    for column in ("name_normalized", "source_release_id", "created_at"):
        op.drop_index(f"ix_healthlabs_food_densities_{column}", table_name="healthlabs_food_densities")
    op.drop_table("healthlabs_food_densities")
