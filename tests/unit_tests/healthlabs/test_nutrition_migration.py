import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.utils.alembic_reader import ALEMBIC_MIGRATIONS, import_file

TABLES = {
    "healthlabs_nutrient_source_releases",
    "healthlabs_nutrient_foods",
    "healthlabs_food_matches",
    "healthlabs_food_portions",
    "healthlabs_branded_products",
}


def _migration():
    path = next(ALEMBIC_MIGRATIONS.glob("*7c91d0e2f4a6*.py"))
    return import_file("add_healthlabs_ingredient_nutrition", path)


def test_nutrition_migration_adds_and_removes_catalog_tables() -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")

    try:
        with engine.connect() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            connection.execute(sa.text("CREATE TABLE users (id CHAR(32) PRIMARY KEY)"))
            connection.execute(sa.text("CREATE TABLE ingredient_foods (id CHAR(32) PRIMARY KEY)"))

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            inspector = sa.inspect(connection)
            assert TABLES <= set(inspector.get_table_names())
            match_foreign_keys = {
                tuple(foreign_key["constrained_columns"]): (
                    foreign_key["referred_table"],
                    foreign_key["options"].get("ondelete"),
                )
                for foreign_key in inspector.get_foreign_keys("healthlabs_food_matches")
            }
            assert match_foreign_keys == {
                ("ingredient_food_id",): ("ingredient_foods", "CASCADE"),
                ("nutrient_food_id",): ("healthlabs_nutrient_foods", "RESTRICT"),
                ("reviewed_by_user_id",): ("users", "SET NULL"),
            }

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()

            assert TABLES.isdisjoint(sa.inspect(connection).get_table_names())
    finally:
        engine.dispose()
