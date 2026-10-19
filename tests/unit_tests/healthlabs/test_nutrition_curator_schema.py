from math import inf, nan

import pytest
from pydantic import ValidationError

from mealie.schema.healthlabs.nutrition_curator import (
    NutritionCuratorCandidateSet,
    NutritionCuratorProviderDecision,
)

FOOD_ID = "11111111-1111-4111-8111-111111111111"
OTHER_FOOD_ID = "22222222-2222-4222-8222-222222222222"
UNIT_ID = "33333333-3333-4333-8333-333333333333"


def _candidate_set(kind: str = "food_match") -> NutritionCuratorCandidateSet:
    candidates = {
        "foods": [
            {"id": FOOD_ID, "label": "Apple, raw"},
            {"id": OTHER_FOOD_ID, "label": "Apple, cooked"},
        ]
    }
    return NutritionCuratorCandidateSet(
        kind=kind,
        target_id="target-1",
        work_input_sha256="1" * 64,
        curator_sha256="2" * 64,
        prompt_payload={
            "kind": kind,
            "targetId": "target-1",
            "target": {"label": "Apple"},
            "candidates": candidates,
        },
        snapshot={"candidates": candidates},
    )


def _decision(**overrides) -> dict:
    return {
        "decision": "select",
        "kind": "food_match",
        "nutrient_food_id": FOOD_ID,
        "confidence": 0.98,
        "runner_up_confidence": 0.2,
        "reason_code": "name_and_context_match",
        "rationale": "I selected the supplied raw apple candidate.",
        **overrides,
    }


def test_candidate_set_is_immutable_forbids_unknown_fields_and_private_snapshot_data() -> None:
    candidate_set = _candidate_set()
    with pytest.raises(ValidationError):
        candidate_set.kind = "portion_resolution"

    with pytest.raises(ValidationError):
        NutritionCuratorCandidateSet(**{**candidate_set.model_dump(), "unexpected": True})

    with pytest.raises(ValidationError):
        NutritionCuratorCandidateSet(
            **{
                **candidate_set.model_dump(exclude={"snapshot"}),
                "snapshot": {"candidates": [{"id": FOOD_ID, "label": "Apple", "grams": 100}]},
            }
        )


@pytest.mark.parametrize("field", ["confidence", "runner_up_confidence"])
@pytest.mark.parametrize("value", [nan, inf, -inf, -0.001, 1.001])
def test_provider_decision_rejects_non_finite_and_out_of_range_scores(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        NutritionCuratorProviderDecision.model_validate(_decision(**{field: value}))


def test_provider_decision_forbids_unknown_fields_and_unknown_reason_codes() -> None:
    with pytest.raises(ValidationError):
        NutritionCuratorProviderDecision.model_validate(_decision(unexpected=True))
    with pytest.raises(ValidationError):
        NutritionCuratorProviderDecision.model_validate(_decision(reason_code="trust_me"))


def test_provider_decision_normalizes_selection_alias() -> None:
    decision = NutritionCuratorProviderDecision.model_validate(_decision(decision="selection"))

    assert decision.decision == "select"


def test_food_match_normalizes_only_redundant_food_id_alias() -> None:
    decision = NutritionCuratorProviderDecision.model_validate(_decision(food_id=FOOD_ID))

    assert decision.food_id is None
    assert str(decision.nutrient_food_id) == FOOD_ID

    with pytest.raises(ValidationError):
        NutritionCuratorProviderDecision.model_validate(_decision(food_id=OTHER_FOOD_ID))


def test_provider_decision_sanitizes_markup_controls_and_whitespace() -> None:
    decision = NutritionCuratorProviderDecision.model_validate(
        _decision(rationale="  <strong>I selected</strong>   Apple, raw.\x00\n")
    )

    assert decision.rationale == "I selected Apple, raw."


def test_provider_decision_rejects_oversized_or_non_first_person_rationale() -> None:
    with pytest.raises(ValidationError):
        NutritionCuratorProviderDecision.model_validate(_decision(rationale="I " + "x" * 280))
    with pytest.raises(ValidationError):
        NutritionCuratorProviderDecision.model_validate(_decision(rationale="The candidate looks correct."))


@pytest.mark.parametrize(
    "payload",
    [
        _decision(decision="insufficient_evidence"),
        _decision(kind="ingredient_structure", unit_id=UNIT_ID),
        _decision(kind="portion_resolution", portion_id=FOOD_ID),
        _decision(
            kind="ingredient_structure",
            nutrient_food_id=None,
            food_id=FOOD_ID,
            unit_id=FOOD_ID,
        ),
    ],
)
def test_provider_decision_rejects_inconsistent_or_duplicate_selections(payload: dict) -> None:
    with pytest.raises(ValidationError):
        NutritionCuratorProviderDecision.model_validate(payload)


def test_abstention_requires_no_selection_and_insufficient_reason() -> None:
    decision = NutritionCuratorProviderDecision.model_validate(
        {
            "decision": "insufficient_evidence",
            "kind": "food_match",
            "confidence": 0,
            "runner_up_confidence": 0,
            "reason_code": "insufficient_candidates",
            "rationale": "I found insufficient compatible evidence.",
        }
    )
    assert decision.nutrient_food_id is None


def test_validation_rejects_unknown_ids_and_explicit_non_supplied_labels() -> None:
    candidate_set = _candidate_set()
    unknown = NutritionCuratorProviderDecision.model_validate(
        _decision(nutrient_food_id="44444444-4444-4444-8444-444444444444")
    )
    with pytest.raises(ValueError, match="supplied candidate"):
        unknown.validated_for(candidate_set)

    unknown_label = NutritionCuratorProviderDecision.model_validate(
        _decision(rationale='I selected "Secret banana" from the candidates.')
    )
    with pytest.raises(ValueError, match="supplied label"):
        unknown_label.validated_for(candidate_set)


def test_validation_accepts_only_kind_specific_supplied_ids() -> None:
    ingredient_candidates = NutritionCuratorCandidateSet(
        kind="ingredient_structure",
        target_id="1",
        work_input_sha256="1" * 64,
        curator_sha256="2" * 64,
        prompt_payload={
            "kind": "ingredient_structure",
            "targetId": "1",
            "target": {"quantity": 2},
            "candidates": {
                "foods": [{"id": FOOD_ID, "label": "Apple"}],
                "units": [{"id": UNIT_ID, "label": "tablespoon"}],
            },
        },
        snapshot={
            "candidates": {
                "foods": [{"id": FOOD_ID, "label": "Apple"}],
                "units": [{"id": UNIT_ID, "label": "tablespoon"}],
            }
        },
    )
    decision = NutritionCuratorProviderDecision.model_validate(
        {
            "decision": "select",
            "kind": "ingredient_structure",
            "food_id": FOOD_ID,
            "unit_id": UNIT_ID,
            "confidence": 0.98,
            "runner_up_confidence": 0.1,
            "reason_code": "name_and_context_match",
            "rationale": "I selected Apple and tablespoon from the supplied candidates.",
        }
    )

    assert decision.validated_for(ingredient_candidates) is decision
