from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from mealie.core.config import get_app_settings
from mealie.core.dependencies.dependencies import get_current_user
from mealie.db.db_setup import generate_session
from mealie.schema.healthlabs.nutrition import (
    HealthLabsFoodMatchWrite,
    HealthLabsFoodNutritionOut,
    HealthLabsFoodNutritionPage,
    HealthLabsFoodPortionCreate,
    HealthLabsNutrientFoodOut,
    HealthLabsNutritionSourceReleaseOut,
    HealthLabsNutritionStatus,
    HealthLabsNutritionWorkerStatus,
    HealthLabsNutritionWorkKind,
    HealthLabsNutritionWorkOut,
    HealthLabsNutritionWorkState,
)
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import RecipeAccessDenied
from mealie.services.healthlabs.idempotency_service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyService,
)
from mealie.services.healthlabs.nutrition_service import (
    NutritionConflict,
    NutritionFoodNotFound,
    NutritionService,
    NutritionSourceNotFound,
)
from mealie.services.healthlabs.nutrition_work_service import NutritionWorkService

router = APIRouter(prefix="/nutrition", tags=["HealthLabs: Nutrition"])


def _service(session: Session, user: PrivateUser) -> NutritionService:
    return NutritionService(session, user)


def _work_service(session: Session, user: PrivateUser) -> NutritionWorkService:
    return NutritionWorkService(
        session,
        user,
        provider_daily_request_limit=get_app_settings().HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT,
    )


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, NutritionFoodNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Food not found") from exc
    if isinstance(exc, NutritionSourceNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nutrition source not found") from exc
    if isinstance(exc, RecipeAccessDenied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, IdempotencyConflict | NutritionConflict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, IdempotencyInProgress):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={"Retry-After": "1"},
        ) from exc
    raise exc


def _idempotent_json(
    *,
    session: Session,
    user: PrivateUser,
    idempotency_key: str,
    request_sha256: str,
    operation: Callable[[], tuple[int, dict]],
) -> JSONResponse:
    result = IdempotencyService(session, user.id).execute_atomic(
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        operation=operation,
    )
    return JSONResponse(status_code=result.status_code, content=result.body)


@router.get("/foods", response_model=HealthLabsFoodNutritionPage)
def list_foods(
    q: str | None = Query(default=None, max_length=255),
    country: str = Query(default="OTHER", min_length=2, max_length=5),
    nutrition_status: HealthLabsNutritionStatus | None = Query(default=None, alias="status"),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> HealthLabsFoodNutritionPage:
    return _service(session, user).list_foods(q=q, country=country, status=nutrition_status)


@router.get("/foods/{food_id}", response_model=HealthLabsFoodNutritionOut)
def get_food(
    food_id: UUID,
    country: str = Query(default="OTHER", min_length=2, max_length=5),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> HealthLabsFoodNutritionOut:
    try:
        return _service(session, user).get_food(food_id, country)
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get("/source-foods", response_model=list[HealthLabsNutrientFoodOut])
def search_source_foods(
    q: str = Query(min_length=2, max_length=255),
    country: str = Query(default="OTHER", min_length=2, max_length=5),
    limit: int = Query(default=20, ge=1, le=50),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> list[HealthLabsNutrientFoodOut]:
    return _service(session, user).search_source_foods(q, country, limit)


@router.put("/foods/{food_id}/match", response_model=HealthLabsFoodNutritionOut)
def set_food_match(
    food_id: UUID,
    data: HealthLabsFoodMatchWrite,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    access_reason: str | None = Header(default=None, alias="X-HealthLabs-Access-Reason", max_length=500),
    break_glass_reason: str | None = Header(default=None, alias="X-HealthLabs-Break-Glass-Reason", max_length=500),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> JSONResponse:
    payload = data.model_dump(mode="json", by_alias=True)
    resource = f"/nutrition/foods/{food_id}/match"
    service = _service(session, user)

    def operation() -> tuple[int, dict]:
        body = service.set_match(
            food_id,
            data,
            access_reason=access_reason,
            break_glass_reason=break_glass_reason,
        ).model_dump(mode="json", by_alias=True)
        return status.HTTP_200_OK, body

    try:
        return _idempotent_json(
            session=session,
            user=user,
            idempotency_key=idempotency_key,
            request_sha256=IdempotencyService.request_hash("PUT", resource, payload),
            operation=operation,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.delete("/foods/{food_id}/match", response_model=HealthLabsFoodNutritionOut)
def clear_food_match(
    food_id: UUID,
    country: str = Query(default="OTHER", min_length=2, max_length=5),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    access_reason: str | None = Header(default=None, alias="X-HealthLabs-Access-Reason", max_length=500),
    break_glass_reason: str | None = Header(default=None, alias="X-HealthLabs-Break-Glass-Reason", max_length=500),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> JSONResponse:
    resource = f"/nutrition/foods/{food_id}/match"
    service = _service(session, user)

    def operation() -> tuple[int, dict]:
        body = service.clear_match(
            food_id,
            country=country,
            access_reason=access_reason,
            break_glass_reason=break_glass_reason,
        ).model_dump(mode="json", by_alias=True)
        return status.HTTP_200_OK, body

    try:
        return _idempotent_json(
            session=session,
            user=user,
            idempotency_key=idempotency_key,
            request_sha256=IdempotencyService.request_hash("DELETE", resource, {}),
            operation=operation,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.post(
    "/foods/{food_id}/portions",
    response_model=HealthLabsFoodNutritionOut,
    status_code=status.HTTP_201_CREATED,
)
def add_food_portion(
    food_id: UUID,
    data: HealthLabsFoodPortionCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    access_reason: str | None = Header(default=None, alias="X-HealthLabs-Access-Reason", max_length=500),
    break_glass_reason: str | None = Header(default=None, alias="X-HealthLabs-Break-Glass-Reason", max_length=500),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> JSONResponse:
    payload = data.model_dump(mode="json", by_alias=True)
    resource = f"/nutrition/foods/{food_id}/portions"
    service = _service(session, user)

    def operation() -> tuple[int, dict]:
        body = service.add_portion(
            food_id,
            data,
            access_reason=access_reason,
            break_glass_reason=break_glass_reason,
        ).model_dump(mode="json", by_alias=True)
        return status.HTTP_201_CREATED, body

    try:
        return _idempotent_json(
            session=session,
            user=user,
            idempotency_key=idempotency_key,
            request_sha256=IdempotencyService.request_hash("POST", resource, payload),
            operation=operation,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get("/sources", response_model=list[HealthLabsNutritionSourceReleaseOut])
def list_sources(
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> list[HealthLabsNutritionSourceReleaseOut]:
    return _service(session, user).list_sources()


@router.get("/worker-status", response_model=HealthLabsNutritionWorkerStatus)
def get_worker_status(
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> HealthLabsNutritionWorkerStatus:
    try:
        return _work_service(session, user).status()
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get("/work", response_model=list[HealthLabsNutritionWorkOut])
def list_work(
    work_state: HealthLabsNutritionWorkState = Query(
        default=HealthLabsNutritionWorkState.REVIEW_REQUIRED,
        alias="state",
    ),
    kind: HealthLabsNutritionWorkKind | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> list[HealthLabsNutritionWorkOut]:
    try:
        return _work_service(session, user).list_work(state=work_state, kind=kind, limit=limit)
    except Exception as exc:
        _raise_http(exc)
        raise
