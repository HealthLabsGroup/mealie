import json
from uuid import uuid4

from mealie.core.config import get_app_settings
from mealie.db.models._model_base import SqlAlchemyBase
from mealie.db.models._model_utils.datetime import NaiveDateTime
from mealie.services.backups_v2.alchemy_exporter import AlchemyExporter
from tests.utils.alembic_reader import alembic_versions


def test_alchemy_exporter():
    settings = get_app_settings()
    exporter = AlchemyExporter(settings.DB_URL)
    data = exporter.dump()

    assert data["alembic_version"] == alembic_versions()
    assert json.dumps(data, indent=4)  # Make sure data is json-serializable

    exporter.engine.dispose()


def test_every_datetime_column_survives_a_backup():
    """Restores rebuild datetimes by column name, so an unregistered one comes back as a string.

    The failure is silent at backup time and only shows up on restore, so this asserts the two stay
    in step rather than waiting for someone to notice a broken restore.
    """
    datetime_columns = {
        column.name
        for table in SqlAlchemyBase.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, NaiveDateTime)
    }

    unregistered = datetime_columns - AlchemyExporter.look_for_datetime
    assert not unregistered, (
        f"datetime columns missing from AlchemyExporter.look_for_datetime: {sorted(unregistered)}. "
        "Add them, or they will restore as strings."
    )


def test_convert_types_preserves_row_around_nested_json():
    settings = get_app_settings()
    exporter = AlchemyExporter(settings.DB_URL)
    operation_id = uuid4()
    recipe_id = uuid4()
    payload = {
        "healthlabs_idempotency_operations": [
            {
                "id": str(operation_id),
                "expires_at": "2026-10-15T12:00:00",
                "response_body": {
                    "id": str(recipe_id),
                    "name": "Soup",
                    "created_at": "2026-10-15T11:00:00",
                },
            }
        ]
    }

    converted = exporter.convert_types(payload)
    restored_row = converted["healthlabs_idempotency_operations"][0]

    assert restored_row["id"] == operation_id.hex
    assert restored_row["expires_at"].isoformat() == "2026-10-15T12:00:00"
    assert restored_row["response_body"] == {
        "id": str(recipe_id),
        "name": "Soup",
        "created_at": "2026-10-15T11:00:00",
    }

    exporter.engine.dispose()
