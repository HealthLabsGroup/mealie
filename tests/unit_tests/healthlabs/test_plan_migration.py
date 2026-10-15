import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.utils.alembic_reader import ALEMBIC_MIGRATIONS, import_file


def _migration():
    path = next(ALEMBIC_MIGRATIONS.glob("*5b82f5da3b27*.py"))
    return import_file("add_healthlabs_plan_publications", path)


def _columns(connection: sa.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns(table)}


def test_plan_migration_adds_publications_without_changing_native_rows() -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")

    try:
        with engine.connect() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            connection.execute(sa.text("CREATE TABLE users (id CHAR(32) PRIMARY KEY)"))
            connection.execute(sa.text("CREATE TABLE recipes (id CHAR(32) PRIMARY KEY)"))
            connection.execute(
                sa.text(
                    "CREATE TABLE healthlabs_recipe_revisions (id CHAR(32) PRIMARY KEY, recipe_id CHAR(32) NOT NULL)"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE TABLE group_meal_plans ("
                    "id INTEGER PRIMARY KEY, date DATE NOT NULL, entry_type VARCHAR NOT NULL)"
                )
            )
            connection.execute(
                sa.text("INSERT INTO group_meal_plans (id, date, entry_type) VALUES (1, '2026-10-15', 'dinner')")
            )

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            inspector = sa.inspect(connection)
            assert inspector.has_table("healthlabs_plan_publications")
            assert inspector.has_table("healthlabs_plan_publication_entries")
            assert inspector.has_table("healthlabs_plan_entry_status")
            assert {
                "healthlabs_publication_entry_id",
                "healthlabs_recipe_revision_id",
            } <= _columns(connection, "group_meal_plans")
            native = connection.execute(
                sa.text(
                    "SELECT healthlabs_publication_entry_id, "
                    "healthlabs_recipe_revision_id FROM group_meal_plans WHERE id = 1"
                )
            ).one()
            assert native == (None, None)

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()

            inspector = sa.inspect(connection)
            assert not inspector.has_table("healthlabs_plan_publications")
            assert not inspector.has_table("healthlabs_plan_publication_entries")
            assert not inspector.has_table("healthlabs_plan_entry_status")
            assert "healthlabs_publication_entry_id" not in _columns(connection, "group_meal_plans")
    finally:
        engine.dispose()
