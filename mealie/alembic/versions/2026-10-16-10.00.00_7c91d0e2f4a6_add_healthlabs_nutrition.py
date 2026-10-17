"""add HealthLabs ingredient nutrition

Revision ID: 7c91d0e2f4a6
Revises: 5b82f5da3b27
Create Date: 2026-10-16 10:00:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "7c91d0e2f4a6"
down_revision: str | None = "5b82f5da3b27"
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
        "healthlabs_nutrient_source_releases",
        *_base_columns(),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("country", sa.String(length=5), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("license_url", sa.String(length=2048), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
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
    op.create_index(
        "ix_healthlabs_nutrient_source_releases_created_at",
        "healthlabs_nutrient_source_releases",
        ["created_at"],
    )
    op.create_index(
        "ix_healthlabs_nutrient_source_releases_provider",
        "healthlabs_nutrient_source_releases",
        ["provider"],
    )

    op.create_table(
        "healthlabs_nutrient_foods",
        *_base_columns(),
        sa.Column("source_release_id", GUID(), nullable=False),
        sa.Column("source_food_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("preparation_state", sa.String(length=100), nullable=True),
        sa.Column("nutrient_values", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_release_id"],
            ["healthlabs_nutrient_source_releases.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_release_id",
            "source_food_id",
            name="healthlabs_nutrient_food_source_key",
        ),
    )
    op.create_index(
        "ix_healthlabs_nutrient_foods_created_at",
        "healthlabs_nutrient_foods",
        ["created_at"],
    )
    op.create_index(
        "ix_healthlabs_nutrient_foods_name",
        "healthlabs_nutrient_foods",
        ["name"],
    )
    op.create_index(
        "ix_healthlabs_nutrient_foods_source_release_id",
        "healthlabs_nutrient_foods",
        ["source_release_id"],
    )

    op.create_table(
        "healthlabs_food_matches",
        *_base_columns(),
        sa.Column("ingredient_food_id", GUID(), nullable=False),
        sa.Column("nutrient_food_id", GUID(), nullable=False),
        sa.Column("country", sa.String(length=5), nullable=False),
        sa.Column("match_method", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("food_group", sa.String(length=32), nullable=True),
        sa.Column("plant_id", sa.String(length=100), nullable=True),
        sa.Column("allergens", sa.JSON(), nullable=True),
        sa.Column("diets", sa.JSON(), nullable=True),
        sa.Column("reviewed_by_user_id", GUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="healthlabs_food_match_confidence_valid",
        ),
        sa.CheckConstraint(
            "(reviewed_by_user_id IS NULL AND reviewed_at IS NULL) OR "
            "(reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="healthlabs_food_match_review_pair_valid",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_food_id"],
            ["ingredient_foods.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["nutrient_food_id"],
            ["healthlabs_nutrient_foods.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ingredient_food_id",
            name="healthlabs_food_match_ingredient_key",
        ),
    )
    for column in ("created_at", "ingredient_food_id", "nutrient_food_id", "reviewed_by_user_id"):
        op.create_index(
            f"ix_healthlabs_food_matches_{column}",
            "healthlabs_food_matches",
            [column],
        )

    op.create_table(
        "healthlabs_food_portions",
        *_base_columns(),
        sa.Column("nutrient_food_id", GUID(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=64), nullable=False),
        sa.Column("grams", sa.Float(), nullable=False),
        sa.Column("source_managed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.CheckConstraint("amount > 0", name="healthlabs_food_portion_amount_positive"),
        sa.CheckConstraint("grams > 0", name="healthlabs_food_portion_grams_positive"),
        sa.ForeignKeyConstraint(
            ["nutrient_food_id"],
            ["healthlabs_nutrient_foods.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "nutrient_food_id",
            "description",
            "amount",
            "unit",
            name="healthlabs_food_portion_identity_key",
        ),
    )
    op.create_index(
        "ix_healthlabs_food_portions_created_at",
        "healthlabs_food_portions",
        ["created_at"],
    )
    op.create_index(
        "ix_healthlabs_food_portions_nutrient_food_id",
        "healthlabs_food_portions",
        ["nutrient_food_id"],
    )

    op.create_table(
        "healthlabs_branded_products",
        *_base_columns(),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("barcode", sa.String(length=64), nullable=False),
        sa.Column("product_name", sa.String(length=500), nullable=False),
        sa.Column("source_version", sa.String(length=100), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("license_url", sa.String(length=2048), nullable=False),
        sa.Column("nutrient_values", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "barcode",
            name="healthlabs_branded_product_provider_barcode_key",
        ),
    )
    op.create_index(
        "ix_healthlabs_branded_products_barcode",
        "healthlabs_branded_products",
        ["barcode"],
    )
    op.create_index(
        "ix_healthlabs_branded_products_created_at",
        "healthlabs_branded_products",
        ["created_at"],
    )
    op.create_index(
        "ix_healthlabs_branded_products_provider",
        "healthlabs_branded_products",
        ["provider"],
    )


def downgrade() -> None:
    for table, indexes in (
        (
            "healthlabs_branded_products",
            (
                "ix_healthlabs_branded_products_provider",
                "ix_healthlabs_branded_products_created_at",
                "ix_healthlabs_branded_products_barcode",
            ),
        ),
        (
            "healthlabs_food_portions",
            ("ix_healthlabs_food_portions_nutrient_food_id", "ix_healthlabs_food_portions_created_at"),
        ),
        (
            "healthlabs_food_matches",
            (
                "ix_healthlabs_food_matches_reviewed_by_user_id",
                "ix_healthlabs_food_matches_nutrient_food_id",
                "ix_healthlabs_food_matches_ingredient_food_id",
                "ix_healthlabs_food_matches_created_at",
            ),
        ),
        (
            "healthlabs_nutrient_foods",
            (
                "ix_healthlabs_nutrient_foods_source_release_id",
                "ix_healthlabs_nutrient_foods_name",
                "ix_healthlabs_nutrient_foods_created_at",
            ),
        ),
        (
            "healthlabs_nutrient_source_releases",
            (
                "ix_healthlabs_nutrient_source_releases_provider",
                "ix_healthlabs_nutrient_source_releases_created_at",
            ),
        ),
    ):
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
