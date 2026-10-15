from datetime import date, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mealie.schema.healthlabs.plan import (
    HealthLabsPlanEntryWrite,
    HealthLabsPlanPublicationWrite,
    plan_content_hash,
)


def _entry(*, day: date, position: int, title: str = "Soup") -> HealthLabsPlanEntryWrite:
    return HealthLabsPlanEntryWrite(
        id=uuid4(),
        date=day,
        entry_type="dinner",
        position=position,
        title=title,
    )


def test_plan_content_hash_is_stable_for_entry_order_and_excludes_expected_publication() -> None:
    start = date(2026, 10, 15)
    first = _entry(day=start, position=0)
    second = _entry(day=start + timedelta(days=1), position=0, title="Beans")
    one = HealthLabsPlanPublicationWrite(
        expected_publication_id=None,
        from_date=start,
        days=7,
        entries=[second, first],
    )
    two = HealthLabsPlanPublicationWrite(
        expected_publication_id=uuid4(),
        from_date=start,
        days=7,
        entries=[first, second],
    )

    assert plan_content_hash(one) == plan_content_hash(two)

    changed = two.model_copy(update={"entries": [first.model_copy(update={"title": "Changed"}), second]})
    assert plan_content_hash(changed) != plan_content_hash(two)


@pytest.mark.parametrize(
    "entry",
    [
        {"entryType": "brunch", "servings": 1, "title": "Invalid type"},
        {"entryType": "dinner", "servings": 0, "title": "Invalid servings"},
        {"entryType": "dinner", "recipeId": str(uuid4()), "title": "Unpinned"},
        {"entryType": "dinner", "title": "   "},
    ],
)
def test_entry_validation_rejects_invalid_operational_content(entry: dict) -> None:
    with pytest.raises(ValidationError):
        HealthLabsPlanEntryWrite.model_validate(
            {
                "id": str(uuid4()),
                "date": "2026-10-15",
                "position": 0,
                **entry,
            }
        )


def test_publication_validation_rejects_out_of_range_duplicate_and_invalid_leftover_entries() -> None:
    start = date(2026, 10, 15)
    source = _entry(day=start, position=0)
    duplicate = _entry(day=start, position=0, title="Duplicate")
    outside = _entry(day=start + timedelta(days=7), position=0, title="Outside")
    missing_leftover = _entry(day=start + timedelta(days=1), position=0, title="Leftovers").model_copy(
        update={"leftover_of_entry_id": uuid4()}
    )

    for entries in ([source, duplicate], [outside], [source, missing_leftover]):
        with pytest.raises(ValidationError):
            HealthLabsPlanPublicationWrite(
                from_date=start,
                days=7,
                entries=entries,
            )
