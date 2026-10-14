from fastapi import APIRouter

from . import provisioning

router = APIRouter(prefix="/healthlabs/v1")
router.include_router(provisioning.router)
