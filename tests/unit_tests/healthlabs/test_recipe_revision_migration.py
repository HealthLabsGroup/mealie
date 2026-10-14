import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.utils.alembic_reader import ALEMBIC_MIGRATIONS, import_file


def _migration():
    path = next(ALEMBIC_MIGRATIONS.glob("*4a71e4c92a16*.py"))
    return import_file("add_healthlabs_recipe_identity", path)


def _columns(connection: sa.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns(table)}


def test_recipe_revision_migration_preserves_ordinary_recipes_with_safe_defaults() -> None:
    migration = _migration()
    engine = sa.create_engine("sqlite://")

    try:
        with engine.connect() as connection:
            connection.execute(sa.text("CREATE TABLE users (id CHAR(32) PRIMARY KEY)"))
            connection.execute(sa.text("CREATE TABLE recipes (id CHAR(32) PRIMARY KEY, name VARCHAR NOT NULL)"))
            connection.execute(sa.text("INSERT INTO users (id) VALUES ('user-before-upgrade')"))
            connection.execute(
                sa.text("INSERT INTO recipes (id, name) VALUES ('recipe-before-upgrade', 'Legacy recipe')")
            )

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            recipe_columns = _columns(connection, "recipes")
            assert {
                "healthlabs_scope",
                "healthlabs_revision_number",
                "copied_from_recipe_id",
                "copied_from_revision_id",
                "copied_at",
            } <= recipe_columns
            migrated = connection.execute(
                sa.text(
                    "SELECT healthlabs_scope, healthlabs_revision_number, copied_from_recipe_id, "
                    "copied_from_revision_id, copied_at FROM recipes WHERE id = 'recipe-before-upgrade'"
                )
            ).one()
            assert migrated == ("personal", 0, None, None, None)
            assert sa.inspect(connection).has_table("healthlabs_recipe_revisions")
            assert sa.inspect(connection).has_table("healthlabs_idempotency_operations")

            connection.execute(
                sa.text("INSERT INTO recipes (id, name) VALUES ('recipe-after-upgrade', 'Ordinary recipe')")
            )
            inserted = connection.execute(
                sa.text(
                    "SELECT healthlabs_scope, healthlabs_revision_number FROM recipes WHERE id = 'recipe-after-upgrade'"
                )
            ).one()
            assert inserted == ("personal", 0)

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()

            assert "healthlabs_scope" not in _columns(connection, "recipes")
            assert not sa.inspect(connection).has_table("healthlabs_recipe_revisions")
            assert not sa.inspect(connection).has_table("healthlabs_idempotency_operations")
    finally:
        engine.dispose()
