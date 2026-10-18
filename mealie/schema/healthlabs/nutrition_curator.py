from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal, Self

from pydantic import UUID4, BaseModel, ConfigDict, Field, field_validator, model_validator

from mealie.schema.openai._base import OpenAIBase

NutritionCuratorKind = Literal["ingredient_structure", "food_match", "portion_resolution"]
ProviderReasonCode = Literal[
    "name_and_context_match",
    "preparation_match",
    "unit_and_context_match",
    "insufficient_candidates",
]

_PRIVATE_CANDIDATE_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "grams",
        "nutrients",
        "nutrientvalues",
        "nutrient_values",
    }
)


class NutritionCuratorCandidateSet(BaseModel):
    kind: NutritionCuratorKind
    target_id: str
    work_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    curator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_payload: dict[str, Any]
    snapshot: dict[str, Any]
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("prompt_payload", "snapshot")
    @classmethod
    def exclude_private_candidate_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key.casefold() in _PRIVATE_CANDIDATE_KEYS:
                        raise ValueError(f"{key} is not permitted in Curator candidate data")
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return value


class NutritionCuratorProviderDecision(OpenAIBase):
    decision: Literal["select", "insufficient_evidence"]
    kind: NutritionCuratorKind
    food_id: UUID4 | None = None
    unit_id: UUID4 | None = None
    nutrient_food_id: UUID4 | None = None
    portion_id: UUID4 | None = None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    runner_up_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason_code: ProviderReasonCode
    rationale: str
    model_config = ConfigDict(extra="forbid")

    @field_validator("rationale", mode="before")
    @classmethod
    def sanitize_rationale(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("rationale must be text")
        value = re.sub(r"<[^>]*>", "", value)
        value = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
        value = " ".join(value.split())
        if not value or len(value) > 280:
            raise ValueError("rationale must contain at most 280 characters")
        if re.search(r"\bI(?:\b|'m|'ve|'d|'ll)", value) is None:
            raise ValueError("rationale must use Apple's concise first-person voice")
        return value

    @model_validator(mode="after")
    def validate_selection_shape(self) -> Self:
        selected = {
            "food_id": self.food_id,
            "unit_id": self.unit_id,
            "nutrient_food_id": self.nutrient_food_id,
            "portion_id": self.portion_id,
        }
        values = [value for value in selected.values() if value is not None]
        if len(values) != len(set(values)):
            raise ValueError("selected IDs must be distinct")

        if self.decision == "insufficient_evidence":
            if values or self.reason_code != "insufficient_candidates":
                raise ValueError("insufficient_evidence cannot contain a selection")
            return self

        expected = {
            "ingredient_structure": {"food_id", "unit_id"},
            "food_match": {"nutrient_food_id"},
            "portion_resolution": {"portion_id"},
        }[self.kind]
        actual = {name for name, value in selected.items() if value is not None}
        if actual != expected or self.reason_code == "insufficient_candidates":
            raise ValueError(f"select requires only the {self.kind} selection fields")
        return self

    def validated_for(self, candidate_set: NutritionCuratorCandidateSet) -> Self:
        if self.kind != candidate_set.kind:
            raise ValueError("decision kind does not match the candidate set")

        candidates = candidate_set.prompt_payload.get("candidates", {})
        field_lists = {
            "food_id": "foods",
            "unit_id": "units",
            "nutrient_food_id": "foods",
            "portion_id": "portions",
        }
        for field, list_name in field_lists.items():
            selected = getattr(self, field)
            if selected is None:
                continue
            supplied_ids = {str(item.get("id")) for item in candidates.get(list_name, [])}
            if str(selected) not in supplied_ids:
                raise ValueError(f"{field} is not a supplied candidate")

        supplied_labels = {
            str(item[key])
            for values in candidates.values()
            for item in values
            for key in ("label", "description", "sourceLabel")
            if item.get(key)
        }
        for quoted_label in re.findall(r'["“]([^"”]+)["”]', self.rationale):
            if quoted_label not in supplied_labels:
                raise ValueError("rationale mentions a non-supplied label")
        return self
