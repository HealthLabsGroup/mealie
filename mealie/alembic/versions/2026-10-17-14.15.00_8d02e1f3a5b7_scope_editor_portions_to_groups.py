"""scope editor portions to groups

Revision ID: 8d02e1f3a5b7
Revises: 7c91d0e2f4a6
Create Date: 2026-10-17 14:15:00.000000
"""

import sqlalchemy as sa

from alembic import op
from mealie.db.migration_types import GUID


revision = "8d02e1f3a5b7"
down_revision: str | None = "7c91d0e2f4a6"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def _portions() -> sa.TableClause:
    return sa.table(
        "healthlabs_food_portions",
        sa.column("id", GUID()),
        sa.column("created_at", sa.DateTime()),
        sa.column("update_at", sa.DateTime()),
        sa.column("nutrient_food_id", GUID()),
        sa.column("group_id", GUID()),
        sa.column("description", sa.String()),
        sa.column("amount", sa.Float()),
        sa.column("unit", sa.String()),
        sa.column("grams", sa.Float()),
        sa.column("source_managed", sa.Boolean()),
    )


def _legacy_editor_owners() -> dict[object, object]:
    portions = _portions()
    matches = sa.table(
        "healthlabs_food_matches",
        sa.column("ingredient_food_id", GUID()),
        sa.column("nutrient_food_id", GUID()),
    )
    foods = sa.table(
        "ingredient_foods",
        sa.column("id", GUID()),
        sa.column("group_id", GUID()),
    )
    connection = op.get_bind()
    editor_ids = set(connection.scalars(sa.select(portions.c.id).where(portions.c.source_managed.is_(False))).all())
    owner_rows = connection.execute(
        sa.select(portions.c.id, foods.c.group_id)
        .select_from(
            portions.join(matches, matches.c.nutrient_food_id == portions.c.nutrient_food_id).join(
                foods, foods.c.id == matches.c.ingredient_food_id
            )
        )
        .where(portions.c.source_managed.is_(False))
        .distinct()
    ).all()
    grouped: dict[object, set[object]] = {portion_id: set() for portion_id in editor_ids}
    for portion_id, group_id in owner_rows:
        grouped[portion_id].add(group_id)

    unresolved = sorted(str(portion_id) for portion_id, group_ids in grouped.items() if len(group_ids) != 1)
    if unresolved:
        raise RuntimeError(
            "Cannot scope legacy editor portions: each must match exactly one workspace; "
            f"resolve portion IDs: {', '.join(unresolved)}"
        )
    return {portion_id: next(iter(group_ids)) for portion_id, group_ids in grouped.items()}


def _backfill_editor_groups(owners: dict[object, object]) -> None:
    portions = _portions()
    connection = op.get_bind()
    for portion_id, group_id in owners.items():
        connection.execute(portions.update().where(portions.c.id == portion_id).values(group_id=group_id))


def upgrade() -> None:
    owners = _legacy_editor_owners()
    with op.batch_alter_table("healthlabs_food_portions") as batch_op:
        batch_op.add_column(sa.Column("group_id", GUID(), nullable=True))
        batch_op.create_foreign_key(
            "fk_healthlabs_food_portions_group_id_groups",
            "groups",
            ["group_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint("healthlabs_food_portion_identity_key", type_="unique")

    _backfill_editor_groups(owners)
    with op.batch_alter_table("healthlabs_food_portions") as batch_op:
        batch_op.create_check_constraint(
            "healthlabs_food_portion_owner_valid",
            "(source_managed AND group_id IS NULL) OR (NOT source_managed AND group_id IS NOT NULL)",
        )
    op.create_index("ix_healthlabs_food_portions_group_id", "healthlabs_food_portions", ["group_id"])
    op.create_index(
        "healthlabs_food_portion_source_key",
        "healthlabs_food_portions",
        ["nutrient_food_id", "description", "amount", "unit"],
        unique=True,
        sqlite_where=sa.text("source_managed = 1"),
        postgresql_where=sa.text("source_managed IS TRUE"),
    )
    op.create_index(
        "healthlabs_food_portion_group_key",
        "healthlabs_food_portions",
        ["group_id", "nutrient_food_id", "description", "amount", "unit"],
        unique=True,
        sqlite_where=sa.text("source_managed = 0"),
        postgresql_where=sa.text("source_managed IS FALSE"),
    )


def downgrade() -> None:
    portions = _portions()
    connection = op.get_bind()
    if connection.scalar(sa.select(sa.func.count()).select_from(portions).where(portions.c.source_managed.is_(False))):
        raise RuntimeError("Cannot downgrade while editor-managed portions still exist; export or remove them first")

    op.drop_index("healthlabs_food_portion_group_key", table_name="healthlabs_food_portions")
    op.drop_index("healthlabs_food_portion_source_key", table_name="healthlabs_food_portions")
    op.drop_index("ix_healthlabs_food_portions_group_id", table_name="healthlabs_food_portions")
    with op.batch_alter_table("healthlabs_food_portions") as batch_op:
        batch_op.drop_constraint("healthlabs_food_portion_owner_valid", type_="check")
        batch_op.drop_constraint("fk_healthlabs_food_portions_group_id_groups", type_="foreignkey")
        batch_op.drop_column("group_id")
        batch_op.create_unique_constraint(
            "healthlabs_food_portion_identity_key",
            ["nutrient_food_id", "description", "amount", "unit"],
        )
