from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import UUID4, Field
from sqlalchemy.orm import Session

from mealie.core.dependencies.healthlabs import require_healthlabs_provisioning_secret
from mealie.db.db_setup import generate_session
from mealie.schema._mealie import MealieModel
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.services.healthlabs.provisioning_service import HealthLabsProvisioningService, ProvisionedCredential

router = APIRouter(
    tags=["HealthLabs: Provisioning"],
    dependencies=[Depends(require_healthlabs_provisioning_secret)],
)


class SubjectProvisionRequest(MealieModel):
    external_subject_id: str = Field(min_length=1, max_length=255)


class CatalogueProvisionRequest(MealieModel):
    access: Literal["reader", "editor"]


class ProvisioningResponse(MealieModel):
    external_subject_id: str
    user_id: UUID4
    group_id: UUID4
    household_id: UUID4
    token: str
    capabilities: list[HealthLabsCapability]


def _response(credential: ProvisionedCredential) -> ProvisioningResponse:
    return ProvisioningResponse(
        external_subject_id=credential.external_subject_id,
        user_id=credential.user_id,
        group_id=credential.group_id,
        household_id=credential.household_id,
        token=credential.token,
        capabilities=list(credential.capabilities),
    )


@router.post("/subjects", response_model=ProvisioningResponse, status_code=status.HTTP_201_CREATED)
def provision_subject(
    data: SubjectProvisionRequest,
    response: Response,
    session: Session = Depends(generate_session),
) -> ProvisioningResponse:
    credential = HealthLabsProvisioningService(session).provision_subject(data.external_subject_id)
    if not credential.created:
        response.status_code = status.HTTP_200_OK
    return _response(credential)


@router.post("/catalogue", response_model=ProvisioningResponse, status_code=status.HTTP_201_CREATED)
def provision_catalogue(
    data: CatalogueProvisionRequest,
    response: Response,
    session: Session = Depends(generate_session),
) -> ProvisioningResponse:
    credential = HealthLabsProvisioningService(session).provision_catalogue(data.access)
    if not credential.created:
        response.status_code = status.HTTP_200_OK
    return _response(credential)


@router.post("/subjects/{external_subject_id}/tokens/rotate", response_model=ProvisioningResponse)
def rotate_subject_token(
    external_subject_id: str,
    session: Session = Depends(generate_session),
) -> ProvisioningResponse:
    try:
        credential = HealthLabsProvisioningService(session).rotate_subject_token(external_subject_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed subject not found") from exc
    return _response(credential)


@router.delete("/subjects/{external_subject_id}", status_code=status.HTTP_204_NO_CONTENT)
def deprovision_subject(
    external_subject_id: str,
    session: Session = Depends(generate_session),
) -> Response:
    try:
        HealthLabsProvisioningService(session).deprovision_subject(external_subject_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed subject not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
