from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from mealie.core.dependencies.dependencies import get_current_user
from mealie.db.db_setup import generate_session
from mealie.lang.providers import Translator, get_locale_provider
from mealie.schema.healthlabs.recipe import HealthLabsImportDraftOut, HealthLabsImportDraftRequest
from mealie.schema.user import PrivateUser
from mealie.services.healthlabs.idempotency_service import IdempotencyService
from mealie.services.healthlabs.import_draft_service import ImportDraftService, map_import_exception

from .recipes import _raise_http

router = APIRouter(tags=["HealthLabs: Import Drafts"])


def _raise_import_http(exc: Exception) -> None:
    if mapped := map_import_exception(exc):
        status_code, detail = mapped
        raise HTTPException(status_code=status_code, detail=detail) from exc
    _raise_http(exc)


@router.post(
    "/import-drafts",
    response_model=HealthLabsImportDraftOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_import_draft(
    data: HealthLabsImportDraftRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> JSONResponse:
    payload = data.model_dump(mode="json", by_alias=True)
    request_hash = IdempotencyService.request_hash("POST", "/import-drafts", payload)
    service = ImportDraftService(session, user, translator)

    async def operation() -> tuple[int, dict]:
        body = (await service.create(data.url)).model_dump(mode="json", by_alias=True)
        return status.HTTP_201_CREATED, body

    try:
        result = await IdempotencyService(session, user.id).execute_async(
            idempotency_key=idempotency_key,
            request_sha256=request_hash,
            operation=operation,
        )
        return JSONResponse(status_code=result.status_code, content=result.body)
    except Exception as exc:
        _raise_import_http(exc)
        raise


@router.get("/import-drafts/{draft_id}", response_model=HealthLabsImportDraftOut)
def get_import_draft(
    draft_id: UUID,
    session: Session = Depends(generate_session),
    user: PrivateUser = Depends(get_current_user),
    translator: Translator = Depends(get_locale_provider),
) -> HealthLabsImportDraftOut:
    try:
        return ImportDraftService(session, user, translator).get(draft_id)
    except Exception as exc:
        _raise_import_http(exc)
        raise
