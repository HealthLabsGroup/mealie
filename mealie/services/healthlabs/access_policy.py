from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from mealie.db.models.healthlabs import HealthLabsAccessAudit, HealthLabsCapabilityGrant
from mealie.schema.healthlabs.identity import HealthLabsCapability
from mealie.schema.user import PrivateUser


class RecipeAccessDenied(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class BreakGlassContext:
    action: str
    reason: str
    subject_external_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None


class RecipeAccessPolicy:
    """Capability policy for HealthLabs-managed recipe operations.

    Mealie's broad administrator bit is deliberately excluded from normal
    content access. An administrator can proceed only through an explicit
    break-glass request, which is flushed to the audit table before access is
    returned to the caller.
    """

    def __init__(self, session: Session, user: PrivateUser) -> None:
        self.session = session
        self.user = user

    def active_capabilities(self) -> set[HealthLabsCapability]:
        values = self.session.scalars(
            select(HealthLabsCapabilityGrant.capability).where(
                HealthLabsCapabilityGrant.user_id == self.user.id,
                HealthLabsCapabilityGrant.revoked_at.is_(None),
            )
        )
        capabilities: set[HealthLabsCapability] = set()
        for value in values:
            try:
                capabilities.add(HealthLabsCapability(value))
            except ValueError:
                # Capability strings are intentionally open-ended in storage so
                # a rolling deploy can introduce a new scope safely.
                continue
        return capabilities

    def allows(self, capability: HealthLabsCapability) -> bool:
        return capability in self.active_capabilities()

    def require(
        self,
        capability: HealthLabsCapability,
        *,
        break_glass: BreakGlassContext | None = None,
    ) -> None:
        if self.allows(capability):
            return

        if not self.user.admin or break_glass is None:
            raise RecipeAccessDenied(f"Missing capability: {capability.value}")

        reason = break_glass.reason.strip()
        action = break_glass.action.strip()
        if not reason or not action:
            raise RecipeAccessDenied("Break-glass access requires an action and reason")

        self.session.add(
            HealthLabsAccessAudit(
                actor_user_id=self.user.id,
                subject_external_id=break_glass.subject_external_id,
                action=action,
                reason=reason,
                resource_type=break_glass.resource_type,
                resource_id=break_glass.resource_id,
            )
        )
        self.session.flush()
