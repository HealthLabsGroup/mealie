import pytest
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


def _portion_scope_migration():
    path = next(ALEMBIC_MIGRATIONS.glob("*8d02e1f3a5b7*.py"))
    return import_file("scope_editor_portions_to_groups", path)


def _worker_migration():
    path = next(ALEMBIC_MIGRATIONS.glob("*9e13f4a6b8c0*.py"))
    return import_file("add_healthlabs_nutrition_work", path)


def _provider_budget_migration():
    path = next(ALEMBIC_MIGRATIONS.glob("*af24c6e18d7b*.py"))
    return import_file("track_healthlabs_provider_cost", path)


def _auto_match_migration():
    path = next(ALEMBIC_MIGRATIONS.glob("*b035d7f29e8c*.py"))
    return import_file("auto_apply_nutrition_matches", path)


def _prepare_legacy_portion(connection, *, matched_groups: int) -> tuple[str, tuple[str, ...]]:
    connection.execute(sa.text("PRAGMA foreign_keys=ON"))
    connection.execute(sa.text("CREATE TABLE users (id CHAR(32) PRIMARY KEY)"))
    connection.execute(sa.text("CREATE TABLE groups (id CHAR(32) PRIMARY KEY)"))
    connection.execute(sa.text("CREATE TABLE ingredient_foods (id CHAR(32) PRIMARY KEY, group_id CHAR(32) NOT NULL)"))
    with Operations.context(MigrationContext.configure(connection)):
        _migration().upgrade()

    source_id = "1" * 32
    nutrient_food_id = "2" * 32
    group_ids = tuple(str(index + 3) * 32 for index in range(matched_groups))
    connection.execute(
        sa.text(
            "INSERT INTO healthlabs_nutrient_source_releases "
            "(id, provider, version, country, source_url, license_url, attribution, sha256, imported_at) "
            "VALUES (:id, 'test', '1', 'GB', 'https://example.test', 'https://example.test', "
            "'test', :sha256, '2026-10-17')"
        ),
        {"id": source_id, "sha256": "a" * 64},
    )
    connection.execute(
        sa.text(
            "INSERT INTO healthlabs_nutrient_foods "
            "(id, source_release_id, source_food_id, name, nutrient_values) "
            "VALUES (:id, :source_id, 'shared', 'Shared food', '{}')"
        ),
        {"id": nutrient_food_id, "source_id": source_id},
    )
    for index, group_id in enumerate(group_ids):
        food_id = str(index + 5) * 32
        connection.execute(sa.text("INSERT INTO groups (id) VALUES (:id)"), {"id": group_id})
        connection.execute(
            sa.text("INSERT INTO ingredient_foods (id, group_id) VALUES (:id, :group_id)"),
            {"id": food_id, "group_id": group_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO healthlabs_food_matches "
                "(id, ingredient_food_id, nutrient_food_id, country, match_method, confidence) "
                "VALUES (:id, :food_id, :nutrient_food_id, 'GB', 'manual', 1)"
            ),
            {"id": food_id, "food_id": food_id, "nutrient_food_id": nutrient_food_id},
        )
    connection.execute(
        sa.text(
            "INSERT INTO healthlabs_food_portions "
            "(id, nutrient_food_id, description, amount, unit, grams, source_managed) "
            "VALUES (:id, :nutrient_food_id, 'editor scoop', 1, 'scoop', 40, 0)"
        ),
        {"id": "7" * 32, "nutrient_food_id": nutrient_food_id},
    )
    connection.execute(
        sa.text(
            "INSERT INTO healthlabs_food_portions "
            "(id, nutrient_food_id, description, amount, unit, grams, source_managed) "
            "VALUES (:id, :nutrient_food_id, 'source cup', 1, 'cup', 200, 1)"
        ),
        {"id": "8" * 32, "nutrient_food_id": nutrient_food_id},
    )
    return nutrient_food_id, group_ids


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
            portion_columns = {column["name"]: column for column in inspector.get_columns("healthlabs_food_portions")}
            assert portion_columns["source_managed"]["nullable"] is False
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


def test_worker_migration_adds_and_removes_persisted_work_tables() -> None:
    migration = _worker_migration()
    engine = sa.create_engine("sqlite://")

    try:
        with engine.connect() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            connection.execute(sa.text("CREATE TABLE groups (id CHAR(32) PRIMARY KEY)"))

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            inspector = sa.inspect(connection)
            assert {"healthlabs_nutrition_work", "healthlabs_nutrition_provider_usage"} <= set(
                inspector.get_table_names()
            )
            assert {index["name"] for index in inspector.get_indexes("healthlabs_nutrition_work")} >= {
                "healthlabs_nutrition_work_due_idx",
                "ix_healthlabs_nutrition_work_group_id",
            }
            assert {
                constraint["name"] for constraint in inspector.get_check_constraints("healthlabs_nutrition_work")
            } >= {
                "healthlabs_nutrition_work_attempt_nonnegative",
                "healthlabs_nutrition_work_kind_valid",
                "healthlabs_nutrition_work_state_valid",
            }
            assert inspector.get_foreign_keys("healthlabs_nutrition_work")[0]["options"]["ondelete"] == "CASCADE"

            connection.execute(sa.text("INSERT INTO groups (id) VALUES ('11111111111111111111111111111111')"))
            connection.execute(
                sa.text(
                    "INSERT INTO healthlabs_nutrition_work "
                    "(id, group_id, kind, target_id, input_sha256, state, attempt_count, next_attempt_at) "
                    "VALUES ('22222222222222222222222222222222', '11111111111111111111111111111111', "
                    "'food_match', '33333333333333333333333333333333', :sha256, 'pending', 0, '2026-10-17')"
                ),
                {"sha256": "a" * 64},
            )
            connection.execute(sa.text("DELETE FROM groups WHERE id = '11111111111111111111111111111111'"))
            assert connection.scalar(sa.text("SELECT COUNT(*) FROM healthlabs_nutrition_work")) == 0

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()

            assert {"healthlabs_nutrition_work", "healthlabs_nutrition_provider_usage"}.isdisjoint(
                sa.inspect(connection).get_table_names()
            )
    finally:
        engine.dispose()


def test_provider_budget_migration_tracks_reserved_cost_and_token_usage() -> None:
    worker_migration = _worker_migration()
    budget_migration = _provider_budget_migration()
    engine = sa.create_engine("sqlite://")

    try:
        with engine.connect() as connection:
            connection.execute(sa.text("CREATE TABLE groups (id CHAR(32) PRIMARY KEY)"))
            with Operations.context(MigrationContext.configure(connection)):
                worker_migration.upgrade()
                budget_migration.upgrade()

            inspector = sa.inspect(connection)
            assert {
                "reserved_microusd",
                "spent_microusd",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
            } <= {column["name"] for column in inspector.get_columns("healthlabs_nutrition_provider_usage")}
            assert {
                constraint["name"]
                for constraint in inspector.get_check_constraints("healthlabs_nutrition_provider_usage")
            } >= {
                "healthlabs_nutrition_provider_usage_reserved_nonnegative",
                "healthlabs_nutrition_provider_usage_spent_nonnegative",
                "healthlabs_nutrition_provider_usage_tokens_nonnegative",
            }

            with Operations.context(MigrationContext.configure(connection)):
                budget_migration.downgrade()
            assert "reserved_microusd" not in {
                column["name"] for column in sa.inspect(connection).get_columns("healthlabs_nutrition_provider_usage")
            }
    finally:
        engine.dispose()


def test_auto_match_migration_preserves_existing_matches_as_unreviewed() -> None:
    migration = _auto_match_migration()
    engine = sa.create_engine("sqlite://")

    try:
        with engine.connect() as connection:
            _prepare_legacy_portion(connection, matched_groups=1)

            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            columns = {
                column["name"]: column for column in sa.inspect(connection).get_columns("healthlabs_food_matches")
            }
            assert columns["auto_applied"]["nullable"] is False
            assert connection.scalar(sa.text("SELECT auto_applied FROM healthlabs_food_matches")) == 0
            assert "healthlabs_food_match_suppressions" in sa.inspect(connection).get_table_names()

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert "healthlabs_food_match_suppressions" not in sa.inspect(connection).get_table_names()
            assert "auto_applied" not in {
                column["name"] for column in sa.inspect(connection).get_columns("healthlabs_food_matches")
            }
    finally:
        engine.dispose()


def test_portion_scope_migration_assigns_an_unambiguous_owner_and_protects_downgrade() -> None:
    scope_migration = _portion_scope_migration()
    engine = sa.create_engine("sqlite://")

    try:
        with engine.connect() as connection:
            _, group_ids = _prepare_legacy_portion(connection, matched_groups=1)

            with Operations.context(MigrationContext.configure(connection)):
                scope_migration.upgrade()

            rows = connection.execute(
                sa.text(
                    "SELECT description, source_managed, group_id FROM healthlabs_food_portions "
                    "ORDER BY description, group_id"
                )
            ).all()
            assert rows == [
                ("editor scoop", 0, group_ids[0]),
                ("source cup", 1, None),
            ]
            assert {index["name"] for index in sa.inspect(connection).get_indexes("healthlabs_food_portions")} >= {
                "healthlabs_food_portion_group_key",
                "healthlabs_food_portion_source_key",
                "ix_healthlabs_food_portions_group_id",
            }
            assert {
                constraint["name"]
                for constraint in sa.inspect(connection).get_check_constraints("healthlabs_food_portions")
            } >= {"healthlabs_food_portion_owner_valid"}

            with pytest.raises(RuntimeError, match="editor-managed portions still exist"):
                with Operations.context(MigrationContext.configure(connection)):
                    scope_migration.downgrade()
            assert connection.scalar(sa.text("SELECT COUNT(*) FROM healthlabs_food_portions")) == 2

            connection.execute(sa.text("DELETE FROM healthlabs_food_portions WHERE source_managed = 0"))
            with Operations.context(MigrationContext.configure(connection)):
                scope_migration.downgrade()
            assert "group_id" not in {
                column["name"] for column in sa.inspect(connection).get_columns("healthlabs_food_portions")
            }
    finally:
        engine.dispose()


@pytest.mark.parametrize("matched_groups", [0, 2])
def test_portion_scope_migration_rejects_unknown_or_ambiguous_legacy_owners(matched_groups: int) -> None:
    scope_migration = _portion_scope_migration()
    engine = sa.create_engine("sqlite://")

    try:
        with engine.connect() as connection:
            _prepare_legacy_portion(connection, matched_groups=matched_groups)

            with pytest.raises(RuntimeError, match="exactly one workspace"):
                with Operations.context(MigrationContext.configure(connection)):
                    scope_migration.upgrade()

            assert "group_id" not in {
                column["name"] for column in sa.inspect(connection).get_columns("healthlabs_food_portions")
            }
    finally:
        engine.dispose()
