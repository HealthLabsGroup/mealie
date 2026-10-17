from collections.abc import Callable
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.models.healthlabs import HealthLabsAccessAudit
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_nutrition_write_is_audited_once_across_idempotent_replay(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_recipe_food: Callable,
    healthlabs_nutrition_source: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    editor, editor_headers = healthlabs_provision()
    healthlabs_set_capabilities(
        editor["userId"],
        HealthLabsCapability.RECIPES_WRITE_OWN,
        HealthLabsCapability.NUTRITION_WRITE,
    )
    _, food_id = healthlabs_recipe_food(editor_headers, f"Audit food {uuid4()}")
    _, nutrient_food_id = healthlabs_nutrition_source(name=f"Audit source {uuid4()}")
    headers = {
        **editor_headers,
        "Idempotency-Key": f"audit-{uuid4()}",
        "X-HealthLabs-Access-Reason": "Ticket NUT-42",
    }
    resource = f"/api/healthlabs/v1/nutrition/foods/{food_id}/match"

    first = api_client.put(resource, json={"nutrientFoodId": nutrient_food_id, "country": "GB"}, headers=headers)
    second = api_client.put(resource, json={"nutrientFoodId": nutrient_food_id, "country": "GB"}, headers=headers)
    assert first.status_code == second.status_code == 200

    audits = unfiltered_database.session.scalars(
        sa.select(HealthLabsAccessAudit).where(
            HealthLabsAccessAudit.actor_user_id == editor["userId"],
            HealthLabsAccessAudit.action == "nutrition.match.set",
        )
    ).all()
    assert len(audits) == 1
    assert audits[0].reason == "Ticket NUT-42"
    assert audits[0].resource_type == "ingredient_food"
    assert audits[0].resource_id == food_id
    with pytest.raises(ValueError, match="immutable"):
        audits[0].reason = "rewritten"
        unfiltered_database.session.commit()
    unfiltered_database.session.rollback()
