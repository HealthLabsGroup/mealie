from fastapi import APIRouter

from . import provisioning, recipes

router = APIRouter(prefix="/healthlabs/v1")
router.include_router(provisioning.router)
router.include_router(recipes.router)
