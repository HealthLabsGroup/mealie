import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from mealie.db.migration_types import GUID
from tests.utils.alembic_reader import ALEMBIC_MIGRATIONS, import_file


def test_scan_migration_roundtrip_and_usage_preservation(connection_url="sqlite://"):
    migration = import_file("metadata_scan", next(ALEMBIC_MIGRATIONS.glob("*recipe_metadata_scan.py")))
    engine = sa.create_engine(connection_url)
    with engine.begin() as connection:
        meta = sa.MetaData()
        sa.Table("healthlabs_recipe_metadata_estimates", meta, sa.Column("id", GUID(), primary_key=True))
        meta.create_all(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.execute(
            sa.text(
                "INSERT INTO healthlabs_recipe_metadata_scans (id,scope_key,created_at,updated_at) "
                "VALUES (:id,:scope,'2026-10-30','2026-10-30')"
            ),
            {"id": "a" * 32, "scope": "b" * 64},
        )
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO healthlabs_recipe_metadata_scans (id,scope_key,created_at,updated_at) "
                        "VALUES (:id,:scope,'2026-10-30','2026-10-30')"
                    ),
                    {"id": "c" * 32, "scope": "b" * 64},
                )
        connection.execute(
            sa.text("INSERT INTO healthlabs_recipe_metadata_estimates (id,usage) VALUES (:id,:usage)"),
            {"id": "c" * 32, "usage": '{"requests":1}'},
        )
        with connection.begin_nested():
            with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="usage"):
                migration.downgrade()
        connection.execute(sa.text("DELETE FROM healthlabs_recipe_metadata_estimates"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "healthlabs_recipe_metadata_scans" not in sa.inspect(connection).get_table_names()
        assert "usage" not in {
            column["name"] for column in sa.inspect(connection).get_columns("healthlabs_recipe_metadata_estimates")
        }
        meta.drop_all(connection)
    engine.dispose()
