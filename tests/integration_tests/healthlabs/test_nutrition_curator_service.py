from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from mealie.db.db_setup import session_context
from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.db.models.healthlabs import (
    HealthLabsAccessAudit,
    HealthLabsCapabilityGrant,
    HealthLabsFoodMatch,
    HealthLabsFoodMatchSuppression,
    HealthLabsFoodPortion,
    HealthLabsFoodPortionMapping,
    HealthLabsNutrientFood,
    HealthLabsNutrientSourceRelease,
    HealthLabsNutritionCuratorDecision,
    HealthLabsNutritionWork,
)
from mealie.db.models.recipe import IngredientFoodModel, RecipeIngredientModel
from mealie.schema.healthlabs.nutrition_curator import NutritionCuratorProviderDecision
from mealie.schema.recipe import Recipe, SaveIngredientFood, SaveIngredientUnit
from mealie.services.healthlabs.nutrition_curator_prompt import NutritionCuratorPrompt
from mealie.services.healthlabs.nutrition_curator_service import NutritionCuratorService
from mealie.services.healthlabs.nutrition_fingerprint_service import NutritionFingerprintService
from mealie.services.healthlabs.nutrition_service import NutritionService
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
        request_headers={"X-Deployment": "test-deployment-v1"},
        request_params={"api-version": "2026-10"},
    )
    session.add(provider)
    session.flush()
    settings.default_provider_id = provider.id
    session.commit()
    return provider


def _source(user: TestUser, *, country: str = "GB") -> HealthLabsNutrientSourceRelease:
    source = HealthLabsNutrientSourceRelease(
        provider=f"source-{uuid4()}",
        version="2026-10",
        country=country,
        source_url="https://source.example/data",
        license_url="https://source.example/licence",
        attribution="Public test source",
        sha256=uuid4().hex.ljust(64, "0"),
    )
    user.repos.session.add(source)
    user.repos.session.flush()
    return source


def _source_food(
    user: TestUser,
    source: HealthLabsNutrientSourceRelease,
    name: str,
    *,
    preparation_state: str | None = None,
) -> HealthLabsNutrientFood:
    food = HealthLabsNutrientFood(
        source_release_id=source.id,
        source_food_id=str(uuid4()),
        name=name,
        preparation_state=preparation_state,
        nutrient_values={"kcal": {"amount": 50, "unit": "kcal"}},
    )
    user.repos.session.add(food)
    user.repos.session.flush()
    return food


def _work(
    user: TestUser,
    kind: str,
    target_id: str,
    input_sha256: str,
) -> HealthLabsNutritionWork:
    work = HealthLabsNutritionWork(
        group_id=user.group_id,
        kind=kind,
        target_id=target_id,
        input_sha256=input_sha256,
        state="review_required",
        next_attempt_at=datetime.now(UTC),
    )
    user.repos.session.add(work)
    user.repos.session.commit()
    return work


def _food_case(
    user: TestUser,
) -> tuple[AIProvider, object, HealthLabsNutrientSourceRelease, HealthLabsNutrientFood, HealthLabsNutritionWork]:
    provider = _provider(user)
    food = user.repos.ingredient_foods.create(
        SaveIngredientFood(group_id=user.group_id, name=f"raw apple {uuid4().hex[:8]}")
    )
    source = _source(user)
    nutrient_food = _source_food(user, source, food.name, preparation_state="raw")
    user.repos.session.commit()
    fingerprint = NutritionFingerprintService(
        user.repos.session,
        country="GB",
        provider_enabled=True,
    ).food(food, None)
    return provider, food, source, nutrient_food, _work(user, "food_match", str(food.id), fingerprint)


def _decision(
    nutrient_food_id: UUID,
    *,
    confidence: float = 0.95,
    runner_up_confidence: float = 0.8,
    rationale: str = "I selected the supplied raw apple candidate.",
) -> NutritionCuratorProviderDecision:
    return NutritionCuratorProviderDecision.model_validate(
        {
            "decision": "select",
            "kind": "food_match",
            "nutrient_food_id": str(nutrient_food_id),
            "confidence": confidence,
            "runner_up_confidence": runner_up_confidence,
            "reason_code": "name_and_context_match",
            "rationale": rationale,
        }
    )


def test_existing_decision_claim_is_compare_and_set_across_stale_sessions(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    _, _, _, _, work = _food_case(user)
    started = NutritionCuratorService(user.repos.session, now=now).start(work.id)
    assert started is not None
    user.repos.session.commit()
    decision = user.repos.session.get(HealthLabsNutritionCuratorDecision, started.decision_id)
    assert decision is not None
    decision.state = "pending"
    decision.next_attempt_at = now
    decision.lease_expires_at = None
    user.repos.session.commit()

    second_session = Session(bind=user.repos.session.get_bind())
    try:
        first_view = user.repos.session.get(HealthLabsNutritionCuratorDecision, decision.id)
        stale_view = second_session.get(HealthLabsNutritionCuratorDecision, decision.id)
        assert first_view is not None and stale_view is not None
        assert NutritionCuratorService(user.repos.session, now=now)._claim_existing(first_view)
        user.repos.session.commit()
        assert not NutritionCuratorService(second_session, now=now)._claim_existing(stale_view)
        second_session.rollback()
    finally:
        second_session.close()

    user.repos.session.refresh(decision)
    assert (decision.state, decision.attempt_count) == ("running", 2)
    user.repos.session.commit()


@pytest.mark.parametrize(
    ("confidence", "runner_up", "expected"),
    [(0.949, 0.1, "insufficient_evidence"), (0.95, 0.801, "insufficient_evidence"), (0.95, 0.8, "approved")],
)
def test_policy_thresholds_are_application_gates(
    unique_user_fn_scoped: TestUser,
    confidence: float,
    runner_up: float,
    expected: str,
) -> None:
    user = unique_user_fn_scoped
    _, food, _, nutrient_food, work = _food_case(user)
    service = NutritionCuratorService(user.repos.session, now=datetime(2026, 10, 18, 12, tzinfo=UTC))
    started = service.start(work.id)
    assert started is not None
    user.repos.session.commit()

    state = service.apply(
        started.decision_id, _decision(nutrient_food.id, confidence=confidence, runner_up_confidence=runner_up), {}
    )
    user.repos.session.commit()

    assert state == expected
    match = user.repos.session.scalar(
        sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
    )
    assert (match is not None) is (expected == "approved")


def test_abstention_unknown_id_and_invalid_response_never_write(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    _, food, _, _, work = _food_case(user)
    service = NutritionCuratorService(user.repos.session)
    started = service.start(work.id)
    assert started is not None
    user.repos.session.commit()
    abstention = NutritionCuratorProviderDecision.model_validate(
        {
            "decision": "insufficient_evidence",
            "kind": "food_match",
            "confidence": 0,
            "runner_up_confidence": 0,
            "reason_code": "insufficient_candidates",
            "rationale": "I found insufficient compatible evidence.",
        }
    )

    assert service.apply(started.decision_id, abstention, {}) == "insufficient_evidence"
    user.repos.session.commit()
    assert (
        user.repos.session.scalar(
            sa.select(HealthLabsFoodMatch.id).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
        )
        is None
    )
    with pytest.raises(ValidationError):
        NutritionCuratorProviderDecision.model_validate(
            {**abstention.model_dump(), "reason_code": "invented", "rationale": "not first person"}
        )

    _, second_food, _, _, second_work = _food_case(user)
    second = service.start(second_work.id)
    assert second is not None
    user.repos.session.commit()
    assert service.apply(second.decision_id, _decision(uuid4()), {}) == "insufficient_evidence"
    user.repos.session.commit()
    assert (
        user.repos.session.scalar(
            sa.select(HealthLabsFoodMatch.id).where(HealthLabsFoodMatch.ingredient_food_id == second_food.id)
        )
        is None
    )


def test_rationale_does_not_change_application_outcome(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    results = []
    for rationale in (
        "I selected the supplied candidate because the name and preparation match.",
        "I calmly chose the only supplied compatible source food.",
    ):
        _, food, _, nutrient_food, work = _food_case(user)
        service = NutritionCuratorService(user.repos.session)
        started = service.start(work.id)
        assert started is not None
        user.repos.session.commit()
        results.append(service.apply(started.decision_id, _decision(nutrient_food.id, rationale=rationale), {}))
        user.repos.session.commit()
        assert (
            user.repos.session.scalar(
                sa.select(HealthLabsFoodMatch.id).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
            )
            is not None
        )

    assert results == ["approved", "approved"]


@pytest.mark.parametrize(
    "mutation",
    [
        "target",
        "candidate",
        "source",
        "catalogue",
        "provider",
        "provider_header",
        "provider_param",
        "capability",
        "reviewed",
        "suppression",
        "ownership",
        "prompt",
    ],
)
def test_apply_revalidates_every_stale_boundary(
    unique_user_fn_scoped: TestUser,
    session: Session,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    user = unique_user_fn_scoped
    provider, food, source, nutrient_food, work = _food_case(user)
    service = NutritionCuratorService(user.repos.session)
    started = service.start(work.id)
    assert started is not None
    user.repos.session.commit()

    if mutation == "target":
        target_food = user.repos.session.get(IngredientFoodModel, food.id)
        assert target_food is not None
        target_food.name = f"changed-{uuid4()}"
    elif mutation == "candidate":
        nutrient_food.name = f"changed-{uuid4()}"
    elif mutation == "source":
        source.sha256 = "f" * 64
    elif mutation == "catalogue":
        user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"extra-{uuid4()}"))
    elif mutation == "provider":
        provider.model = "test/model-v2"
    elif mutation == "provider_header":
        provider.request_headers[0].value = "test-deployment-v2"
    elif mutation == "provider_param":
        provider.request_params[0].value = "2026-11"
    elif mutation == "capability":
        grant = user.repos.session.scalar(
            sa.select(HealthLabsCapabilityGrant).where(HealthLabsCapabilityGrant.user_id == started.agent_user_id)
        )
        assert grant is not None
        grant.revoked_at = datetime.now(UTC)
    elif mutation == "reviewed":
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
    elif mutation == "suppression":
        user.repos.session.add(HealthLabsFoodMatchSuppression(ingredient_food_id=food.id))
    elif mutation == "ownership":
        other = build_unique_user(session, "", api_client)
        target_food = user.repos.session.get(IngredientFoodModel, food.id)
        assert target_food is not None
        target_food.group_id = other.group_id
    else:
        monkeypatch.setattr(NutritionCuratorPrompt, "system", staticmethod(lambda: "changed prompt"))
    user.repos.session.commit()

    assert service.apply(started.decision_id, _decision(nutrient_food.id), {}) == "insufficient_evidence"
    user.repos.session.commit()
    decision = user.repos.session.get(HealthLabsNutritionCuratorDecision, started.decision_id)
    assert decision is not None and decision.reason_code == "stale_fingerprint"
    match = user.repos.session.scalar(
        sa.select(HealthLabsFoodMatch).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
    )
    assert match is None or match.reviewed_by_user_id == user.user_id


def test_food_apply_rejects_concurrent_target_edit(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = unique_user_fn_scoped
    _, food, _, nutrient_food, work = _food_case(user)
    service = NutritionCuratorService(user.repos.session)
    started = service.start(work.id)
    assert started is not None
    user.repos.session.commit()
    original = NutritionService.set_match

    def edit_before_set_match(self, food_id, data, **kwargs):
        with session_context() as concurrent:
            current = concurrent.get(IngredientFoodModel, food_id)
            assert current is not None
            current.name = f"raw beef {uuid4().hex[:8]}"
            concurrent.commit()
        return original(self, food_id, data, **kwargs)

    monkeypatch.setattr(NutritionService, "set_match", edit_before_set_match)

    assert service.apply(started.decision_id, _decision(nutrient_food.id), {}) == "insufficient_evidence"
    user.repos.session.commit()
    assert (
        user.repos.session.scalar(
            sa.select(HealthLabsFoodMatch.id).where(HealthLabsFoodMatch.ingredient_food_id == food.id)
        )
        is None
    )


def test_food_apply_serializes_edit_after_fingerprint_check(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = unique_user_fn_scoped
    if user.repos.session.bind is None or user.repos.session.bind.dialect.name != "sqlite":
        pytest.skip("SQLite write-lock regression")
    _, food, _, nutrient_food, work = _food_case(user)
    service = NutritionCuratorService(user.repos.session)
    started = service.start(work.id)
    assert started is not None
    user.repos.session.commit()
    original_name = food.name
    original = NutritionService._authorize_write
    blocked = False

    def edit_before_authorize(self, food_id, *args, **kwargs):
        nonlocal blocked
        # Keep ownership through rollback so the timeout is restored before
        # this connection can return to the shared pool.
        with user.repos.session.get_bind().connect() as connection:
            previous = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            try:
                connection.exec_driver_sql("PRAGMA busy_timeout = 0")
                with Session(bind=connection) as concurrent:
                    current = concurrent.get(IngredientFoodModel, food_id)
                    assert current is not None
                    current.name = f"raw beef {uuid4().hex[:8]}"
                    try:
                        concurrent.commit()
                    except OperationalError:
                        blocked = True
                        concurrent.rollback()
            finally:
                connection.exec_driver_sql(f"PRAGMA busy_timeout = {previous}")
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == previous
        return original(self, food_id, *args, **kwargs)

    monkeypatch.setattr(NutritionService, "_authorize_write", edit_before_authorize)

    assert service.apply(started.decision_id, _decision(nutrient_food.id), {}) == "approved"
    user.repos.session.commit()
    assert blocked
    user.repos.session.expire_all()
    assert user.repos.session.get(IngredientFoodModel, food.id).name == original_name


def test_food_apply_preserves_human_review_before_write_lock(
    unique_user_fn_scoped: TestUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = unique_user_fn_scoped
    _, food, _, nutrient_food, work = _food_case(user)
    match = HealthLabsFoodMatch(
        ingredient_food_id=food.id,
        nutrient_food_id=nutrient_food.id,
        country="GB",
        match_method="provider_ranked",
        confidence=0.9,
    )
    user.repos.session.add(match)
    user.repos.session.flush()
    work.input_sha256 = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True).food(
        food, match
    )
    user.repos.session.commit()
    service = NutritionCuratorService(user.repos.session)
    started = service.start(work.id)
    assert started is not None
    user.repos.session.commit()
    original_execute = user.repos.session.execute
    injected = False

    def review_before_food_lock(statement, *args, **kwargs):
        nonlocal injected
        if not injected and getattr(statement, "is_update", False) and statement.table.name == "ingredient_foods":
            with session_context() as concurrent:
                current = concurrent.get(HealthLabsFoodMatch, match.id)
                assert current is not None
                current.match_method = "manual"
                current.reviewed_by_user_id = user.user_id
                current.reviewed_at = datetime.now(UTC)
                concurrent.commit()
            injected = True
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(user.repos.session, "execute", review_before_food_lock)

    assert service.apply(started.decision_id, _decision(nutrient_food.id), {}) == "insufficient_evidence"
    user.repos.session.commit()
    user.repos.session.expire_all()
    preserved = user.repos.session.get(HealthLabsFoodMatch, match.id)
    assert injected
    assert preserved is not None
    assert (preserved.match_method, preserved.reviewed_by_user_id) == ("manual", user.user_id)


def test_start_is_unique_and_empty_candidates_finalize_without_provider(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    _provider(user)
    recipe = user.repos.recipes.create(
        Recipe(
            user_id=user.user_id,
            group_id=user.group_id,
            household_id=user.household_id,
            name=f"Empty candidate recipe {uuid4()}",
        )
    )
    ingredient = RecipeIngredientModel(
        session=user.repos.session,
        group_id=user.group_id,
        recipe_id=recipe.id,
        position=0,
        original_text=f"apple-{uuid4()}",
    )
    user.repos.session.add(ingredient)
    user.repos.session.commit()
    fingerprint = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True).ingredient(
        ingredient, user.group_id
    )
    work = _work(user, "ingredient_structure", str(ingredient.id), fingerprint)
    service = NutritionCuratorService(user.repos.session)

    finalized = service.start(work.id)
    assert finalized is not None and finalized.state == "insufficient_evidence"
    decision = user.repos.session.scalar(
        sa.select(HealthLabsNutritionCuratorDecision).where(HealthLabsNutritionCuratorDecision.work_id == work.id)
    )
    assert decision is not None and decision.state == "insufficient_evidence"
    assert service.start(work.id) is None
    user.repos.session.commit()

    _, _, _, _, work_with_candidate = _food_case(user)
    first = service.start(work_with_candidate.id)
    assert first is not None
    assert service.start(work_with_candidate.id) is None
    user.repos.session.commit()


def test_ingredient_selection_is_same_group_compare_and_set_and_preserves_text(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    _provider(user)
    token = uuid4().hex[:8]
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"apple {token}"))
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"tablespoon {token}"))
    recipe = user.repos.recipes.create(
        Recipe(user_id=user.user_id, group_id=user.group_id, household_id=user.household_id, name=f"Recipe {token}")
    )
    ingredient = RecipeIngredientModel(
        session=user.repos.session,
        group_id=user.group_id,
        recipe_id=recipe.id,
        position=0,
        original_text=f"2 {unit.name} {food.name}",
        note="keep this note",
    )
    user.repos.session.add(ingredient)
    user.repos.session.commit()
    fingerprints = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True)
    work = _work(user, "ingredient_structure", str(ingredient.id), fingerprints.ingredient(ingredient, user.group_id))
    service = NutritionCuratorService(user.repos.session)
    started = service.start(work.id)
    assert started is not None
    original_text = ingredient.original_text
    user.repos.session.commit()
    response = NutritionCuratorProviderDecision.model_validate(
        {
            "decision": "select",
            "kind": "ingredient_structure",
            "food_id": str(food.id),
            "unit_id": str(unit.id),
            "confidence": 0.97,
            "runner_up_confidence": 0.2,
            "reason_code": "name_and_context_match",
            "rationale": "I selected the supplied food and unit from the ingredient context.",
        }
    )

    assert service.apply(started.decision_id, response, {}) == "approved"
    user.repos.session.commit()
    user.repos.session.refresh(ingredient)
    assert (ingredient.food_id, ingredient.unit_id, ingredient.quantity) == (food.id, unit.id, 2)
    assert ingredient.original_text == original_text
    assert ingredient.note == "keep this note"


def test_food_selection_is_audited_agent_review_and_preserves_optional_metadata(
    unique_user_fn_scoped: TestUser,
) -> None:
    user = unique_user_fn_scoped
    _, food, _, nutrient_food, work = _food_case(user)
    existing = HealthLabsFoodMatch(
        ingredient_food_id=food.id,
        nutrient_food_id=nutrient_food.id,
        country="GB",
        match_method="ambiguous",
        confidence=0.7,
        food_group="fruit",
        plant_id="malus",
        allergens={"apple": "possible"},
        diets={"vegan": "yes"},
    )
    user.repos.session.add(existing)
    user.repos.session.flush()
    work.input_sha256 = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True).food(
        food, existing
    )
    user.repos.session.commit()
    service = NutritionCuratorService(user.repos.session)
    started = service.start(work.id)
    assert started is not None
    user.repos.session.commit()

    assert service.apply(started.decision_id, _decision(nutrient_food.id), {}) == "approved"
    user.repos.session.commit()
    user.repos.session.refresh(existing)
    assert existing.match_method == "agent"
    assert existing.reviewed_by_user_id == started.agent_user_id
    assert existing.reviewed_at is not None
    assert (existing.food_group, existing.plant_id, existing.allergens, existing.diets) == (
        "fruit",
        "malus",
        {"apple": "possible"},
        {"vegan": "yes"},
    )
    audit = user.repos.session.scalar(
        sa.select(HealthLabsAccessAudit).where(HealthLabsAccessAudit.actor_user_id == started.agent_user_id)
    )
    assert audit is not None and audit.reason == f"Nutrition curator decision {started.decision_id}"


def test_portion_selection_maps_existing_row_and_drives_calculation(unique_user_fn_scoped: TestUser) -> None:
    user = unique_user_fn_scoped
    _provider(user)
    food = user.repos.ingredient_foods.create(SaveIngredientFood(group_id=user.group_id, name=f"apple-{uuid4()}"))
    unit = user.repos.ingredient_units.create(SaveIngredientUnit(group_id=user.group_id, name=f"scoop-{uuid4()}"))
    nutrient_food = _source_food(user, _source(user), food.name)
    match = HealthLabsFoodMatch(
        ingredient_food_id=food.id,
        nutrient_food_id=nutrient_food.id,
        country="GB",
        match_method="manual",
        confidence=1,
        reviewed_by_user_id=user.user_id,
        reviewed_at=datetime.now(UTC),
    )
    portion = HealthLabsFoodPortion(
        nutrient_food_id=nutrient_food.id,
        description="one piece",
        amount=1,
        unit="piece",
        grams=123,
    )
    later_portion = HealthLabsFoodPortion(
        nutrient_food_id=nutrient_food.id,
        description="large piece",
        amount=1,
        unit="piece",
        grams=456,
    )
    recipe = user.repos.recipes.create(
        Recipe(user_id=user.user_id, group_id=user.group_id, household_id=user.household_id, name=f"Recipe {uuid4()}")
    )
    ingredient = RecipeIngredientModel(
        session=user.repos.session,
        group_id=user.group_id,
        recipe_id=recipe.id,
        position=0,
        original_text="2 scoops apple",
        food_id=food.id,
        unit_id=unit.id,
        quantity=2,
    )
    later_ingredient = RecipeIngredientModel(
        session=user.repos.session,
        group_id=user.group_id,
        recipe_id=recipe.id,
        position=1,
        original_text="3 scoops apple",
        food_id=food.id,
        unit_id=unit.id,
        quantity=3,
    )
    user.repos.session.add_all([match, portion, later_portion, ingredient, later_ingredient])
    user.repos.session.commit()
    fingerprint = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True).portion(
        ingredient, match, user.group_id
    )
    work = _work(user, "portion_resolution", str(ingredient.id), fingerprint)
    later_fingerprint = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True).portion(
        later_ingredient, match, user.group_id
    )
    later_work = _work(user, "portion_resolution", str(later_ingredient.id), later_fingerprint)
    service = NutritionCuratorService(user.repos.session)
    started = service.start(work.id)
    assert started is not None
    later_started = service.start(later_work.id)
    assert later_started is not None
    before_count = user.repos.session.scalar(sa.select(sa.func.count()).select_from(HealthLabsFoodPortion))
    user.repos.session.commit()
    response = NutritionCuratorProviderDecision.model_validate(
        {
            "decision": "select",
            "kind": "portion_resolution",
            "portion_id": str(portion.id),
            "confidence": 0.99,
            "runner_up_confidence": 0.1,
            "reason_code": "unit_and_context_match",
            "rationale": "I selected the supplied existing portion for this food and unit.",
        }
    )

    assert service.apply(started.decision_id, response, {}) == "approved"
    user.repos.session.commit()
    later_response = response.model_copy(update={"portion_id": later_portion.id})
    assert service.apply(later_started.decision_id, later_response, {}) == "insufficient_evidence"
    user.repos.session.commit()
    mapping = user.repos.session.scalar(
        sa.select(HealthLabsFoodPortionMapping).where(
            HealthLabsFoodPortionMapping.group_id == user.group_id,
            HealthLabsFoodPortionMapping.ingredient_food_id == food.id,
            HealthLabsFoodPortionMapping.ingredient_unit_id == unit.id,
        )
    )
    assert mapping is not None
    assert (mapping.food_portion_id, mapping.reviewed_by_user_id, mapping.curator_decision_id) == (
        portion.id,
        started.agent_user_id,
        started.decision_id,
    )
    assert user.repos.session.scalar(sa.select(sa.func.count()).select_from(HealthLabsFoodPortion)) == before_count
    assert NutritionService(user.repos.session).food_facts(food.id, unit.id, 2).resolved_grams == 246

    replacement_food = _source_food(user, _source(user), f"replacement-{food.name}")
    replacement_portion = HealthLabsFoodPortion(
        nutrient_food_id=replacement_food.id,
        description="replacement piece",
        amount=1,
        unit="piece",
        grams=321,
    )
    user.repos.session.add(replacement_portion)
    match.nutrient_food_id = replacement_food.id
    user.repos.session.flush()
    work.input_sha256 = NutritionFingerprintService(user.repos.session, country="GB", provider_enabled=True).portion(
        ingredient, match, user.group_id
    )
    work.state = "review_required"
    user.repos.session.commit()
    assert NutritionService(user.repos.session).food_facts(food.id, unit.id, 2).resolved_grams is None

    replacement_started = service.start(work.id)
    assert replacement_started is not None
    user.repos.session.commit()
    replacement_response = response.model_copy(update={"portion_id": replacement_portion.id})
    assert service.apply(replacement_started.decision_id, replacement_response, {}) == "approved"
    user.repos.session.commit()
    user.repos.session.refresh(mapping)
    assert mapping.food_portion_id == replacement_portion.id
    assert NutritionService(user.repos.session).food_facts(food.id, unit.id, 2).resolved_grams == 642
