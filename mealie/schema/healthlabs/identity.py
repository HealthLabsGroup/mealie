from datetime import datetime
from enum import StrEnum

from pydantic import UUID4, ConfigDict, Field

from mealie.schema._mealie import MealieModel


class HealthLabsCapability(StrEnum):
    RECIPES_READ_OWN = "recipes:read_own"
    RECIPES_WRITE_OWN = "recipes:write_own"
    RECIPES_READ_ALL = "recipes:read_all"
    PLANS_READ_OWN = "plans:read_own"
    PLANS_MARK_COOKED_OWN = "plans:mark_cooked_own"
    PLANS_WRITE_ALL = "plans:write_all"
    CATALOGUE_READ = "catalogue:read"
    CATALOGUE_WRITE = "catalogue:write"


class HealthLabsIdentity(MealieModel):
    external_subject_id: str = Field(min_length=1, max_length=255)
    user_id: UUID4


class HealthLabsCapabilityGrantOut(MealieModel):
    id: UUID4
    user_id: UUID4
    capability: HealthLabsCapability
    granted_by_user_id: UUID4 | None = None
    reason: str
    granted_at: datetime
    revoked_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


class HealthLabsAccessAuditOut(MealieModel):
    id: UUID4
    actor_user_id: UUID4 | None = None
    subject_external_id: str | None = None
    action: str
    reason: str
    resource_type: str | None = None
    resource_id: str | None = None
    occurred_at: datetime
    model_config = ConfigDict(from_attributes=True)
