from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from mealie.core.dependencies.dependencies import get_current_user
from mealie.db.db_setup import generate_session
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import RecipeAccessPolicy

router = APIRouter(tags=["HealthLabs: Identity"])


@router.get("/capabilities", response_model=list[HealthLabsCapability])
def get_current_capabilities(
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> list[HealthLabsCapability]:
    return sorted(RecipeAccessPolicy(session, user).active_capabilities(), key=lambda capability: capability.value)
