from fastapi import APIRouter

from . import import_drafts, provisioning, recipes

router = APIRouter(prefix="/healthlabs/v1")
router.include_router(provisioning.router)
router.include_router(recipes.router)
router.include_router(import_drafts.router)
