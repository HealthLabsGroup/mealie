from fastapi import APIRouter

from . import identity, import_drafts, nutrition, plans, provisioning, recipes

router = APIRouter(prefix="/healthlabs/v1")
router.include_router(provisioning.router)
router.include_router(identity.router)
router.include_router(recipes.router)
router.include_router(nutrition.router)
router.include_router(import_drafts.router)
router.include_router(plans.router)
