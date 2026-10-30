from uuid import UUID, uuid4

from mealie.db.models.healthlabs import HealthLabsNutrientFood
from mealie.services.healthlabs.recipe_metadata_service import RecipeMetadataService
from tests.unit_tests.healthlabs.test_nutrition_calculation import CORE_VALUES, _source
from tests.utils import TestUser


def test_candidates_without_preparation_rank_exact_length_name_and_id_with_country_scope(
    unique_user_fn_scoped: TestUser,
):
    user = unique_user_fn_scoped
    name = f"grain{uuid4().hex}"
    base_id = uuid4().int & ~255
    gb, other, foreign = (_source(user, country=country) for country in ("GB", "OTHER", "FR"))
    # IDs deliberately oppose insertion order; equal names use IDs only as the final tie-breaker.
    rows = [
        (5, gb, f"{name} longer", None),
        (4, other, f"{name} b", None),
        (3, gb, f"{name} a", None),
        (2, other, name, "cooked"),
        (1, gb, name, "raw"),
        (6, foreign, name, None),
    ]
    for identifier, source, label, state in rows:
        user.repos.session.add(
            HealthLabsNutrientFood(
                id=UUID(int=base_id + identifier),
                source_release_id=source.id,
                source_food_id=str(uuid4()),
                name=label,
                preparation_state=state,
                nutrient_values=CORE_VALUES,
            )
        )
    user.repos.session.commit()

    service = RecipeMetadataService(user.repos.session)
    candidates = service._candidates(name, group_id=user.group_id)

    assert [candidate.id for candidate in candidates] == [UUID(int=base_id + value) for value in (1, 2, 3, 4, 5)]
    assert [candidate.id for candidate in service._candidates(name, preparation_state="cooked")] == [
        UUID(int=base_id + 2)
    ]
