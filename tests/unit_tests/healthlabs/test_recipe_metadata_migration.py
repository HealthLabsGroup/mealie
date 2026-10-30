import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from mealie.db.migration_types import GUID
from tests.utils.alembic_reader import ALEMBIC_MIGRATIONS, import_file


def test_metadata_migration_enforces_job_states_identity_and_preserves_evidence(connection_url="sqlite://"):
    paths = list(ALEMBIC_MIGRATIONS.glob("*recipe_metadata_estimates.py"))
    assert len(paths) == 1
    migration = import_file("recipe_metadata_estimates", paths[0])
    engine = sa.create_engine(connection_url)
    with engine.begin() as connection:
        parents = sa.MetaData()
        for table in ("groups", "recipes"):
            sa.Table(table, parents, sa.Column("id", GUID(), primary_key=True))
        parents.create_all(connection)
        connection.execute(sa.text("INSERT INTO groups (id) VALUES ('22222222222222222222222222222222')"))
        connection.execute(sa.text("INSERT INTO recipes (id) VALUES ('33333333333333333333333333333333')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        inspector = sa.inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("healthlabs_recipe_metadata_estimates")}
        assert {
            "authoring_fingerprint",
            "lease_token",
            "lease_expires_at",
            "attempt_count",
            "next_attempt_at",
            "last_error_code",
            "updated_at",
        } <= columns
        insert = sa.text(
            "INSERT INTO healthlabs_recipe_metadata_estimates "
            "(id, group_id, recipe_id, input_fingerprint, authoring_fingerprint, "
            "policy_version, state, next_attempt_at, created_at, updated_at) "
            "VALUES (:id, :gid, :rid, :fp, :fp, 'v1', :state, '2026-10-30', '2026-10-30', '2026-10-30')"
        )
        params = {"id": "1" * 32, "gid": "2" * 32, "rid": "3" * 32, "fp": "a" * 64, "state": "pending"}
        connection.execute(insert, params)
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(insert, {**params, "id": "4" * 32})
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(insert, {**params, "id": "4" * 32, "fp": "b" * 64, "state": "invented"})
        for state in ("pending", "running", "ready", "rejected", "failed", "superseded"):
            connection.execute(
                sa.text("UPDATE healthlabs_recipe_metadata_estimates SET state=:state"), {"state": state}
            )
        with connection.begin_nested():
            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="evidence"),
            ):
                migration.downgrade()
        connection.execute(sa.text("DELETE FROM healthlabs_recipe_metadata_estimates"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "healthlabs_recipe_metadata_estimates" not in sa.inspect(connection).get_table_names()
        parents.drop_all(connection)
    engine.dispose()
