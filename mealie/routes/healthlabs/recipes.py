from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from mealie.core import exceptions as mealie_exceptions
from mealie.core.dependencies.dependencies import get_current_user
from mealie.db.db_setup import generate_session
from mealie.lang.providers import Translator, get_locale_provider
from mealie.schema.healthlabs.recipe import (
    HealthLabsOperationOut,
    HealthLabsRecipeOut,
    HealthLabsRecipePage,
    HealthLabsRecipeRevisionOut,
    HealthLabsRecipeWrite,
)
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.access_policy import RecipeAccessDenied
from mealie.services.healthlabs.idempotency_service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyService,
)
from mealie.services.healthlabs.recipe_api_service import (
    IMAGE_MIME_FORMATS,
    MAX_IMAGE_BYTES,
    HealthLabsInvalidImage,
    HealthLabsRecipeNotFound,
    RecipeApiService,
)

router = APIRouter(tags=["HealthLabs: Recipes"])


def _service(session: Session, user: PrivateUser, translator: Translator) -> RecipeApiService:
    return RecipeApiService(session, user, translator)


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, HealthLabsRecipeNotFound | mealie_exceptions.NoEntryFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe not found") from exc
    if isinstance(exc, RecipeAccessDenied | mealie_exceptions.PermissionDenied):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, IdempotencyConflict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, IdempotencyInProgress):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc), headers={"Retry-After": "1"}
        ) from exc
    if isinstance(exc, HealthLabsInvalidImage):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    raise exc


def _idempotent_json(
    *,
    session: Session,
    user: PrivateUser,
    idempotency_key: str,
    request_sha256: str,
    operation,
) -> JSONResponse:
    if len(idempotency_key) > 255:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Idempotency key is too long")
    result = IdempotencyService(session, user.id).execute(
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        operation=operation,
    )
    return JSONResponse(status_code=result.status_code, content=result.body)


@router.get("/recipes", response_model=HealthLabsRecipePage)
def list_recipes(
    q: str | None = Query(default=None, max_length=255),
    cursor: str | None = Query(default=None, max_length=64),
    access_reason: str | None = Header(default=None, alias="X-HealthLabs-Access-Reason"),
    break_glass_reason: str | None = Header(default=None, alias="X-HealthLabs-Break-Glass-Reason"),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> HealthLabsRecipePage:
    try:
        return _service(session, user, translator).list_recipes(
            q=q,
            cursor=cursor,
            access_reason=access_reason,
            break_glass_reason=break_glass_reason,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.post("/recipes", response_model=HealthLabsRecipeOut, status_code=status.HTTP_201_CREATED)
def create_recipe(
    data: HealthLabsRecipeWrite,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> JSONResponse:
    payload = data.model_dump(mode="json", by_alias=True)
    request_hash = IdempotencyService.request_hash("POST", "/recipes", payload)
    service = _service(session, user, translator)

    def operation() -> tuple[int, dict]:
        body = service.create_recipe(data).model_dump(mode="json", by_alias=True)
        return status.HTTP_201_CREATED, body

    try:
        return _idempotent_json(
            session=session,
            user=user,
            idempotency_key=idempotency_key,
            request_sha256=request_hash,
            operation=operation,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get("/recipes/{recipe_id}", response_model=HealthLabsRecipeOut)
def get_recipe(
    recipe_id: UUID,
    access_reason: str | None = Header(default=None, alias="X-HealthLabs-Access-Reason"),
    break_glass_reason: str | None = Header(default=None, alias="X-HealthLabs-Break-Glass-Reason"),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> HealthLabsRecipeOut:
    try:
        return _service(session, user, translator).get_recipe(
            recipe_id,
            access_reason=access_reason,
            break_glass_reason=break_glass_reason,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.put("/recipes/{recipe_id}", response_model=HealthLabsRecipeOut)
def update_recipe(
    recipe_id: UUID,
    data: HealthLabsRecipeWrite,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> JSONResponse:
    payload = data.model_dump(mode="json", by_alias=True)
    resource = f"/recipes/{recipe_id}"
    request_hash = IdempotencyService.request_hash("PUT", resource, payload)
    service = _service(session, user, translator)

    def operation() -> tuple[int, dict]:
        body = service.update_recipe(recipe_id, data).model_dump(mode="json", by_alias=True)
        return status.HTTP_200_OK, body

    try:
        return _idempotent_json(
            session=session,
            user=user,
            idempotency_key=idempotency_key,
            request_sha256=request_hash,
            operation=operation,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.delete("/recipes/{recipe_id}", response_model=HealthLabsRecipeOut)
def delete_recipe(
    recipe_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> JSONResponse:
    resource = f"/recipes/{recipe_id}"
    request_hash = IdempotencyService.request_hash("DELETE", resource, {})
    service = _service(session, user, translator)

    def operation() -> tuple[int, dict]:
        body = service.delete_recipe(recipe_id).model_dump(mode="json", by_alias=True)
        return status.HTTP_200_OK, body

    try:
        return _idempotent_json(
            session=session,
            user=user,
            idempotency_key=idempotency_key,
            request_sha256=request_hash,
            operation=operation,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get(
    "/recipes/{recipe_id}/revisions/{revision_id}",
    response_model=HealthLabsRecipeRevisionOut,
)
def get_recipe_revision(
    recipe_id: UUID,
    revision_id: UUID,
    access_reason: str | None = Header(default=None, alias="X-HealthLabs-Access-Reason"),
    break_glass_reason: str | None = Header(default=None, alias="X-HealthLabs-Break-Glass-Reason"),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> HealthLabsRecipeRevisionOut:
    try:
        return _service(session, user, translator).get_revision(
            recipe_id,
            revision_id,
            access_reason=access_reason,
            break_glass_reason=break_glass_reason,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.post("/recipes/{recipe_id}/image", response_model=HealthLabsRecipeOut)
async def update_recipe_image(
    recipe_id: UUID,
    image: UploadFile = File(...),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> JSONResponse:
    content_type = (image.content_type or "").lower()
    if content_type not in IMAGE_MIME_FORMATS:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Unsupported image content type")
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Recipe image is too large")

    resource = f"/recipes/{recipe_id}/image"
    request_hash = IdempotencyService.request_hash(
        "POST",
        resource,
        {"contentType": content_type, "contentSha256": hashlib.sha256(data).hexdigest()},
    )
    service = _service(session, user, translator)

    def operation() -> tuple[int, dict]:
        body = service.update_recipe_image(recipe_id, data, content_type).model_dump(mode="json", by_alias=True)
        return status.HTTP_200_OK, body

    try:
        return _idempotent_json(
            session=session,
            user=user,
            idempotency_key=idempotency_key,
            request_sha256=request_hash,
            operation=operation,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get("/recipes/{recipe_id}/image", response_class=Response)
def get_recipe_image(
    recipe_id: UUID,
    access_reason: str | None = Header(default=None, alias="X-HealthLabs-Access-Reason"),
    break_glass_reason: str | None = Header(default=None, alias="X-HealthLabs-Break-Glass-Reason"),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> Response:
    try:
        data = _service(session, user, translator).read_recipe_image(
            recipe_id,
            access_reason=access_reason,
            break_glass_reason=break_glass_reason,
        )
        return Response(content=data, media_type="image/webp", headers={"Cache-Control": "private, no-store"})
    except Exception as exc:
        _raise_http(exc)
        raise


@router.delete("/recipes/{recipe_id}/image", response_model=HealthLabsRecipeOut)
def delete_recipe_image(
    recipe_id: UUID,
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> HealthLabsRecipeOut:
    try:
        return _service(session, user, translator).delete_recipe_image(recipe_id)
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get("/operations/{idempotency_key}", response_model=HealthLabsOperationOut)
def get_operation(
    idempotency_key: str,
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
) -> HealthLabsOperationOut:
    operation = IdempotencyService(session, user.id).get(idempotency_key)
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found")
    return HealthLabsOperationOut(
        idempotency_key=operation.idempotency_key,
        state=operation.state,
        request_sha256=operation.request_sha256,
        response_status=operation.response_status,
        response_body=operation.response_body,
        completed_at=operation.completed_at,
        expires_at=operation.expires_at,
    )
