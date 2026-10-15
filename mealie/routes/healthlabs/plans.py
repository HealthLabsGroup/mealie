from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from mealie.core.dependencies.dependencies import get_current_user
from mealie.db.db_setup import generate_session
from mealie.schema.healthlabs.plan import (
    HealthLabsPlanEntryStatusPatch,
    HealthLabsPlanPublicationEntryOut,
    HealthLabsPlanPublicationOut,
    HealthLabsPlanPublicationWrite,
)
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import RecipeAccessDenied
from mealie.services.healthlabs.idempotency_service import IdempotencyConflict, IdempotencyInProgress
from mealie.services.healthlabs.plan_api_service import (
    HealthLabsPlanConflict,
    HealthLabsPlanNotFound,
    PlanApiService,
)

router = APIRouter(tags=["HealthLabs: Plans"])


def _service(session: Session, user: PrivateUser) -> PlanApiService:
    return PlanApiService(session, user)


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, HealthLabsPlanNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found") from exc
    if isinstance(exc, RecipeAccessDenied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, IdempotencyInProgress):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={"Retry-After": "1"},
        ) from exc
    if isinstance(exc, HealthLabsPlanConflict | IdempotencyConflict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    raise exc


@router.get("/plans/current", response_model=HealthLabsPlanPublicationOut | None)
def get_own_current_plan(
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> HealthLabsPlanPublicationOut | None:
    try:
        return _service(session, user).get_own_current()
    except Exception as exc:
        _raise_http(exc)
        raise


@router.patch(
    "/plans/{plan_id}/entries/{entry_id}",
    response_model=HealthLabsPlanPublicationEntryOut,
)
def patch_own_plan_entry(
    plan_id: UUID,
    entry_id: UUID,
    data: HealthLabsPlanEntryStatusPatch,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> HealthLabsPlanPublicationEntryOut:
    try:
        return _service(session, user).patch_own_entry(
            plan_id,
            entry_id,
            data,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get(
    "/subjects/{external_subject_id}/plans/current",
    response_model=HealthLabsPlanPublicationOut | None,
)
def get_subject_current_plan(
    external_subject_id: str,
    access_reason: str | None = Header(
        default=None,
        alias="X-HealthLabs-Access-Reason",
        max_length=500,
    ),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> HealthLabsPlanPublicationOut | None:
    try:
        return _service(session, user).get_subject_current(
            external_subject_id,
            access_reason=access_reason,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.post(
    "/subjects/{external_subject_id}/plan-publications",
    response_model=HealthLabsPlanPublicationOut,
    status_code=status.HTTP_201_CREATED,
)
def publish_subject_plan(
    external_subject_id: str,
    data: HealthLabsPlanPublicationWrite,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    access_reason: str | None = Header(
        default=None,
        alias="X-HealthLabs-Access-Reason",
        max_length=500,
    ),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> HealthLabsPlanPublicationOut:
    try:
        return _service(session, user).publish(
            external_subject_id,
            data,
            idempotency_key=idempotency_key,
            access_reason=access_reason,
        )
    except Exception as exc:
        _raise_http(exc)
        raise
