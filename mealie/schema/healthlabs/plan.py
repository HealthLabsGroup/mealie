from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import UUID4, ConfigDict, Field, model_validator

from mealie.schema._mealie import MealieModel


class HealthLabsPlanEntryType(StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class HealthLabsPlanEntryWrite(MealieModel):
    id: UUID4
    date: date
    entry_type: HealthLabsPlanEntryType
    position: int = Field(ge=0)
    servings: float | None = Field(default=None, gt=0)
    recipe_id: UUID4 | None = None
    recipe_revision_id: UUID4 | None = None
    title: str | None = Field(default=None, max_length=255)
    leftover_of_entry_id: UUID4 | None = None

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if (self.recipe_id is None) != (self.recipe_revision_id is None):
            raise ValueError("recipeId and recipeRevisionId must be provided together")
        if self.recipe_id is None:
            self.title = (self.title or "").strip()
            if not self.title:
                raise ValueError("Plain plan entries require a title")
        elif self.title is not None:
            self.title = self.title.strip() or None
        return self


class HealthLabsPlanPublicationWrite(MealieModel):
    expected_publication_id: UUID4 | None = None
    from_date: date = Field(alias="from")
    days: Literal[3, 7, 14]
    entries: list[HealthLabsPlanEntryWrite]

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        by_id = {entry.id: entry for entry in self.entries}
        if len(by_id) != len(self.entries):
            raise ValueError("Plan entry IDs must be unique")

        slots = {(entry.date, entry.entry_type, entry.position) for entry in self.entries}
        if len(slots) != len(self.entries):
            raise ValueError("Plan entry positions must be unique within a meal slot")

        last_date = self.from_date + timedelta(days=self.days - 1)
        for entry in self.entries:
            if not self.from_date <= entry.date <= last_date:
                raise ValueError("Plan entry date is outside the publication window")
            if entry.leftover_of_entry_id is None:
                continue
            source = by_id.get(entry.leftover_of_entry_id)
            if source is None or source.id == entry.id:
                raise ValueError("Leftover source must be another entry in this publication")
            if source.leftover_of_entry_id is not None or source.recipe_id is None:
                raise ValueError("Leftover source must be a recipe entry, not another leftover")
            if entry.recipe_id != source.recipe_id or entry.recipe_revision_id != source.recipe_revision_id:
                raise ValueError("Leftover recipe and revision must match its source")
            if entry.date <= source.date:
                raise ValueError("Leftovers must be scheduled after their source")
        return self


class HealthLabsPlanPublicationEntryOut(MealieModel):
    id: UUID4
    date: date
    entry_type: HealthLabsPlanEntryType
    position: int
    servings: float | None = None
    recipe_id: UUID4 | None = None
    recipe_revision_id: UUID4 | None = None
    title: str
    leftover_of_entry_id: UUID4 | None = None
    recipe_snapshot: dict[str, Any] | None = None
    cooked: bool = False
    cooked_at: datetime | None = None
    version: int = 1


class HealthLabsPlanEntryStatusPatch(MealieModel):
    version: int = Field(ge=1)
    cooked: bool
    model_config = ConfigDict(extra="forbid")


class HealthLabsPlanPublicationOut(MealieModel):
    id: UUID4
    publication_number: int
    content_sha256: str
    from_date: date = Field(alias="from")
    days: Literal[3, 7, 14]
    published_at: datetime
    idempotency_key: str
    entries: list[HealthLabsPlanPublicationEntryOut]


def plan_content_hash(data: HealthLabsPlanPublicationWrite) -> str:
    payload = data.model_dump(
        mode="json",
        by_alias=True,
        exclude={"expected_publication_id"},
    )
    payload["entries"] = sorted(
        payload["entries"],
        key=lambda entry: (
            entry["date"],
            entry["entryType"],
            entry["position"],
            entry["id"],
        ),
    )
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()
