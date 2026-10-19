from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsFoodMatch,
    HealthLabsFoodPortion,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import RecipeIngredientModel
from mealie.schema.recipe import Recipe, SaveIngredientFood, SaveIngredientUnit
from mealie.services.healthlabs.nutrition_curator_candidates import NutritionCuratorCandidateService
from mealie.services.healthlabs.nutrition_curator_prompt import NutritionCuratorPrompt
from tests.fixtures.fixture_users import build_unique_user
from tests.utils import TestUser


def _provider(user: TestUser) -> AIProvider:
    session = user.repos.session
    settings = session.scalar(sa.select(AIProviderSettings).where(AIProviderSettings.group_id == user.group_id))
    assert settings is not None
    provider = AIProvider(
        session=session,
        settings_id=settings.id,
        name=f"curator-{uuid4()}",
        base_url="https://provider.example/v1",
        api_key="offline-test-key",
        model="test/model-v1",
        timeout=30,
    )
    session.add(provider)
    session.commit()
    return provider


def _work(user: TestUser, kind: str, target_id: str) -> HealthLabsNutritionWork:
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind=kind,
        target_id=target_id,
        input_sha256=uuid4().hex.ljust(64, "0"),
        state="review_required",
        next_attempt_at=datetime.now(UTC),
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    return work


def _ingredient(
    user: TestUser,
    original_text: str,
    *,
    food_id=None,
    unit_id=None,
    quantity: float | None = None,
) -> RecipeIngredientModel:
    session = user.repos.session
    recipe = user.repos.recipes.create(
        Recipe(
            user_id=user.user_id,
            group_id=user.group_id,
            household_id=user.household_id,
            name=f"Candidate recipe {uuid4()}",
        )
    )
    ingredient = RecipeIngredientModel(
        session=session,
        group_id=user.group_id,
        recipe_id=recipe.id,
        position=0,
        original_text=original_text,
        food_id=food_id,
        unit_id=unit_id,
        quantity=quantity,
    )
    session.add(ingredient)
    session.commit()
    return ingredient


def _source(user: TestUser, country: str) -> HealthLabsNutrientSourceRelease:
    release = HealthLabsNutrientSourceRelease(
        provider=f"source-{country}-{uuid4()}",
        version="2026-10",
        country=country,
        source_url="https://source.example/data",
        license_url="https://source.example/licence",
        attribution="Public test source",
        sha256=uuid4().hex.ljust(64, "0"),
    )
    user.repos.session.add(release)
    user.repos.session.flush()
    return release


def _source_food(
    user: TestUser,
    release: HealthLabsNutrientSourceRelease,
    name: str,
    preparation_state: str | None = None,
) -> HealthLabsNutrientFood:
    food = HealthLabsNutrientFood(
        source_release_id=release.id,
        source_food_id=str(uuid4()),
        name=name,
        preparation_state=preparation_state,
        nutrient_values={"secret_nutrient": {"amount": 999, "unit": "g"}},
    )
    user.repos.session.add(food)
    user.repos.session.flush()
    return food


def test_ingredient_candidates_put_exact_first_cap_lists_and_stay_in_group(
    unique_user_fn_scoped: TestUser,
    session: Session,
    api_client: TestClient,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex[:8]
    exact_food = user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=f"green apple {token}", aliases=[{"name": f"pomme {token}"}])
    )
    exact_unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=user.group_id, name=f"tablespoon {token}", aliases=[{"name": f"tbsp {token}"}])
    )
    for index in range(10):
        user.repos.ingredient_foods.create(
            SaveIngredientFood(group_id=user.group_id, name=f"green apple {token} candidate {index:02d}")
        )
        user.repos.ingredient_units.create(
            SaveIngredientUnit(group_id=user.group_id, name=f"tablespoon {token} candidate {index:02d}")
        )
    other = build_unique_user(session, "", api_client)
    foreign_food = other.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=other.group_id, name=exact_food.name)
    )
    foreign_unit = other.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=other.group_id, name=exact_unit.name)
    )
    ingredient = _ingredient(user, f"2 {exact_unit.name} {exact_food.name}")

    candidates = NutritionCuratorCandidateService(user.repos.session).build(
        _work(user, "ingredient_structure", str(ingredient.id)),
        _provider(user),
    )

    assert candidates is not None
    foods = candidates.prompt_payload["candidates"]["foods"]
    units = candidates.prompt_payload["candidates"]["units"]
    assert foods[0]["id"] == str(exact_food.id)
    assert units[0]["id"] == str(exact_unit.id)
    assert len(foods) == len(units) == 8
    assert str(foreign_food.id) not in {item["id"] for item in foods}
    assert str(foreign_unit.id) not in {item["id"] for item in units}
    assert foods[1:] == sorted(foods[1:], key=lambda item: (-item["score"], item["label"], item["id"]))


def test_ingredient_candidates_require_positive_quantity_and_exclude_ambiguous_aliases(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    alias = f"shared-{uuid4()}"
    foods = [
        user.repos.ingredient_foods.create(
            SaveIngredientFood(group_id=user.group_id, name=f"food-{uuid4()}", aliases=[{"name": alias}])
        )
        for _ in range(2)
    ]
    units = [
        user.repos.ingredient_units.create(
            SaveIngredientUnit(group_id=user.group_id, name=f"unit-{uuid4()}", aliases=[{"name": alias}])
        )
        for _ in range(2)
    ]
    no_quantity = _ingredient(user, f"{alias} {alias}")
    candidate_set = NutritionCuratorCandidateService(user.repos.session).build(
        _work(user, "ingredient_structure", str(no_quantity.id)),
        _provider(user),
    )

    assert candidate_set is not None
    assert candidate_set.prompt_payload["candidates"] == {"foods": [], "units": []}

    positive = _ingredient(user, f"2 {alias} {alias}")
    candidate_set = NutritionCuratorCandidateService(user.repos.session).build(
        _work(user, "ingredient_structure", str(positive.id)),
        _provider(user),
    )
    assert candidate_set is not None
    candidate_ids = {item["id"] for values in candidate_set.prompt_payload["candidates"].values() for item in values}
    assert candidate_ids.isdisjoint({str(item.id) for item in [*foods, *units]})


def test_ingredient_candidates_use_sanitized_work_suggestion_without_exposing_note(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex[:8]
    food = user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=f"suggested apple {token}")
    )
    unit = user.repos.ingredient_units.create(
        SaveIngredientUnit(group_id=user.group_id, name=f"suggested spoon {token}")
    )
    ingredient = _ingredient(user, "")
    ingredient.note = f"private note {token}"
    work = _work(user, "ingredient_structure", str(ingredient.id))
    work.result = {
        "reason": "ingredient_suggestion",
        "suggestion": {"quantity": 2, "food": food.name, "unit": unit.name, "note": "sanitized"},
    }
    user.repos.session.commit()

    candidate_set = NutritionCuratorCandidateService(user.repos.session).build(work, _provider(user))

    assert candidate_set is not None
    assert candidate_set.prompt_payload["candidates"]["foods"][0]["id"] == str(food.id)
    assert candidate_set.prompt_payload["candidates"]["units"][0]["id"] == str(unit.id)
    assert token not in candidate_set.prompt_payload["target"]["input"]
    assert "private note" not in candidate_set.model_dump_json()


def test_food_candidates_are_country_preparation_scoped_capped_and_public(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex[:8]
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"raw apple {token}"))
    gb = _source(user, "GB")
    other = _source(user, "OTHER")
    fr = _source(user, "FR")
    allowed = [_source_food(user, gb, f"raw apple {token} {index:02d}", "raw") for index in range(10)]
    allowed.append(_source_food(user, other, f"raw apple {token} other", "raw"))
    wrong_country = _source_food(user, fr, f"raw apple {token}", "raw")
    wrong_preparation = _source_food(user, gb, f"raw apple {token}", "cooked")
    user.repos.session.commit()

    candidate_set = NutritionCuratorCandidateService(user.repos.session, country="GB").build(
        _work(user, "food_match", str(food.id)),
        _provider(user),
    )

    assert candidate_set is not None
    candidates = candidate_set.prompt_payload["candidates"]["foods"]
    assert len(candidates) == 8
    assert {item["country"] for item in candidates} <= {"GB", "OTHER"}
    assert all(item["countryCompatible"] is True for item in candidates)
    assert all(item["preparationState"] in {None, "raw"} for item in candidates)
    assert str(wrong_country.id) not in {item["id"] for item in candidates}
    assert str(wrong_preparation.id) not in {item["id"] for item in candidates}
    assert {item["id"] for item in candidates} <= {str(item.id) for item in allowed}
    serialized = candidate_set.model_dump_json()
    assert "secret_nutrient" not in serialized
    assert "nutrient_values" not in serialized


def test_food_candidate_specificity_breaks_token_subset_score_ties(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex[:8]
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"lettuce {token}"))
    release = _source(user, "GB")
    direct = _source_food(user, release, f"lettuce {token}, average, raw", "raw")
    incidental = _source_food(user, release, f"sandwich with lettuce {token} and tomato")
    user.repos.session.commit()

    candidate_set = NutritionCuratorCandidateService(user.repos.session, country="GB").build(
        _work(user, "food_match", str(food.id)),
        _provider(user),
    )

    assert candidate_set is not None
    candidates = candidate_set.prompt_payload["candidates"]["foods"]
    assert [item["score"] for item in candidates[:2]] == [100, 100]
    assert candidates[0]["id"] == str(direct.id)
    assert candidates[1]["id"] == str(incidental.id)
    assert candidates[0]["specificityScore"] > candidates[1]["specificityScore"]


def test_portion_candidates_use_current_match_and_never_expose_grams(
    unique_user_fn_scoped: TestUser,
    session: Session,
    api_client: TestClient,
) -> None:
    user = unique_user_fn_scoped
    other_user = build_unique_user(session, "", api_client)
    token = uuid4().hex[:8]
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"apple {token}"))
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"piece {token}"))
    release = _source(user, "GB")
    nutrient_food = _source_food(user, release, food.name)
    other_nutrient_food = _source_food(user, release, f"pear {token}")
    user.repos.session.add(
        HealthLabsFoodMatch(
            ingredient_food_id=food.id,
            nutrient_food_id=nutrient_food.id,
            country="GB",
            match_method="manual",
            confidence=1,
            reviewed_by_user_id=user.user_id,
            reviewed_at=datetime.now(UTC),
        )
    )
    source_portion = HealthLabsFoodPortion(
        nutrient_food_id=nutrient_food.id,
        description="one piece",
        amount=1,
        unit="piece",
        grams=123,
        source_managed=True,
    )
    own_portion = HealthLabsFoodPortion(
        nutrient_food_id=nutrient_food.id,
        group_id=user.group_id,
        description="small piece",
        amount=1,
        unit="piece",
        grams=100,
        source_managed=False,
    )
    foreign_portion = HealthLabsFoodPortion(
        nutrient_food_id=nutrient_food.id,
        group_id=other_user.group_id,
        description="foreign piece",
        amount=1,
        unit="piece",
        grams=999,
        source_managed=False,
    )
    unrelated_portion = HealthLabsFoodPortion(
        nutrient_food_id=other_nutrient_food.id,
        description="unrelated piece",
        amount=1,
        unit="piece",
        grams=999,
        source_managed=True,
    )
    user.repos.session.add_all([source_portion, own_portion, foreign_portion, unrelated_portion])
    user.repos.session.commit()
    ingredient = _ingredient(user, f"1 {unit.name} {food.name}", food_id=food.id, unit_id=unit.id, quantity=1)

    candidate_set = NutritionCuratorCandidateService(user.repos.session).build(
        _work(user, "portion_resolution", str(ingredient.id)),
        _provider(user),
    )

    assert candidate_set is not None
    candidates = candidate_set.prompt_payload["candidates"]["portions"]
    assert {item["id"] for item in candidates} == {str(source_portion.id), str(own_portion.id)}
    assert str(foreign_portion.id) not in {item["id"] for item in candidates}
    assert str(unrelated_portion.id) not in {item["id"] for item in candidates}
    assert "grams" not in candidate_set.model_dump_json()


def test_curator_fingerprint_covers_context_but_not_candidate_order(
    unique_user_fn_scoped: TestUser,
    monkeypatch,
) -> None:
    user = unique_user_fn_scoped
    token = uuid4().hex[:8]
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"apple {token}"))
    release = _source(user, "GB")
    original_source_sha = release.sha256
    source_food = _source_food(user, release, f"apple {token}")
    user.repos.session.commit()
    work = _work(user, "food_match", str(food.id))
    provider = _provider(user)
    service = NutritionCuratorCandidateService(user.repos.session)

    baseline = service.build(work, provider)
    repeated = service.build(work, provider)
    assert baseline is not None and repeated is not None
    assert repeated.curator_sha256 == baseline.curator_sha256
    assert repeated.reuse_sha256 == baseline.reuse_sha256

    other_target_payload = deepcopy(baseline.prompt_payload)
    other_target_payload["targetId"] = str(uuid4())
    other_curator_sha256, other_reuse_sha256 = service._fingerprints(
        work,
        provider,
        other_target_payload,
        baseline.snapshot,
    )
    assert other_curator_sha256 != baseline.curator_sha256
    assert other_reuse_sha256 == baseline.reuse_sha256

    provider.api_key = "rotated-offline-test-key"
    user.repos.session.commit()
    rotated_credentials = service.build(work, provider)
    assert rotated_credentials is not None and rotated_credentials.curator_sha256 == baseline.curator_sha256

    provider.model = "test/model-v2"
    user.repos.session.commit()
    changed_provider = service.build(work, provider)
    assert changed_provider is not None and changed_provider.curator_sha256 != baseline.curator_sha256
    assert changed_provider.reuse_sha256 != baseline.reuse_sha256
    provider.model = "test/model-v1"
    release.sha256 = "f" * 64
    user.repos.session.commit()
    changed_source = service.build(work, provider)
    assert changed_source is not None and changed_source.curator_sha256 != baseline.curator_sha256
    assert changed_source.reuse_sha256 != baseline.reuse_sha256

    release.sha256 = original_source_sha
    user.repos.session.commit()
    restored = NutritionCuratorCandidateService(user.repos.session).build(work, provider)
    assert restored is not None and restored.curator_sha256 == baseline.curator_sha256
    user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"catalogue {uuid4()}"))
    changed_catalogue = NutritionCuratorCandidateService(user.repos.session).build(work, provider)
    assert changed_catalogue is not None and changed_catalogue.curator_sha256 != restored.curator_sha256

    monkeypatch.setattr(NutritionCuratorPrompt, "system", staticmethod(lambda: "changed persona prompt"))
    changed_persona = NutritionCuratorCandidateService(user.repos.session).build(work, provider)
    assert changed_persona is not None and changed_persona.curator_sha256 != changed_catalogue.curator_sha256

    source_food.name = f"renamed {token}"
    user.repos.session.commit()
    changed_candidates = NutritionCuratorCandidateService(user.repos.session).build(work, provider)
    assert changed_candidates is not None and changed_candidates.curator_sha256 != changed_persona.curator_sha256
