from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mealie.db.models._model_utils.datetime import get_utc_now
from mealie.db.models.healthlabs import HealthLabsIdempotencyOperation


class IdempotencyConflict(RuntimeError):
    pass


class IdempotencyInProgress(RuntimeError):
    pass


@dataclass(frozen=True)
class IdempotentResponse:
    status_code: int
    body: dict[str, Any]
    replayed: bool


class IdempotencyService:
    RETENTION = timedelta(hours=24)
    WAIT_SECONDS = 10.0

    def __init__(self, session: Session, actor_user_id: UUID) -> None:
        self.session = session
        self.actor_user_id = actor_user_id

    @staticmethod
    def request_hash(method: str, resource: str, body: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"method": method.upper(), "resource": resource, "body": body},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    def execute(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        operation: Callable[[], tuple[int, dict[str, Any]]],
    ) -> IdempotentResponse:
        while True:
            existing = self._find(idempotency_key)
            if existing is not None:
                resolved = self._resolve_existing(existing, request_sha256)
                if resolved is not None:
                    return resolved
                continue

            row = HealthLabsIdempotencyOperation(
                actor_user_id=self.actor_user_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                state="pending",
                expires_at=get_utc_now() + self.RETENTION,
            )
            self.session.add(row)
            try:
                self.session.commit()
            except IntegrityError:
                self.session.rollback()
                continue

            try:
                status_code, body = operation()
            except Exception:
                self.session.rollback()
                self.session.execute(
                    sa.delete(HealthLabsIdempotencyOperation).where(
                        HealthLabsIdempotencyOperation.id == row.id,
                        HealthLabsIdempotencyOperation.state == "pending",
                    )
                )
                self.session.commit()
                raise

            persisted = self.session.get(HealthLabsIdempotencyOperation, row.id)
            if persisted is None:
                raise RuntimeError("Idempotency operation disappeared before completion")
            persisted.state = "completed"
            persisted.response_status = status_code
            persisted.response_body = body
            persisted.completed_at = get_utc_now()
            persisted.expires_at = get_utc_now() + self.RETENTION
            self.session.commit()
            return IdempotentResponse(status_code=status_code, body=body, replayed=False)

    def get(self, idempotency_key: str) -> HealthLabsIdempotencyOperation | None:
        row = self._find(idempotency_key)
        if row is not None and row.expires_at <= get_utc_now():
            self.session.delete(row)
            self.session.commit()
            return None
        return row

    def _find(self, idempotency_key: str) -> HealthLabsIdempotencyOperation | None:
        return self.session.scalar(
            sa.select(HealthLabsIdempotencyOperation).where(
                HealthLabsIdempotencyOperation.actor_user_id == self.actor_user_id,
                HealthLabsIdempotencyOperation.idempotency_key == idempotency_key,
            )
        )

    def _resolve_existing(
        self,
        row: HealthLabsIdempotencyOperation,
        request_sha256: str,
    ) -> IdempotentResponse | None:
        if row.expires_at <= get_utc_now():
            self.session.delete(row)
            self.session.commit()
            return None
        if row.request_sha256 != request_sha256:
            raise IdempotencyConflict("Idempotency key was already used for a different request")
        if row.state == "completed":
            if row.response_status is None or row.response_body is None:
                raise RuntimeError("Completed idempotency operation has no response")
            return IdempotentResponse(status_code=row.response_status, body=row.response_body, replayed=True)

        deadline = time.monotonic() + self.WAIT_SECONDS
        while time.monotonic() < deadline:
            self.session.rollback()
            time.sleep(0.02)
            refreshed = self._find(row.idempotency_key)
            if refreshed is None:
                return None
            if refreshed.request_sha256 != request_sha256:
                raise IdempotencyConflict("Idempotency key was already used for a different request")
            if refreshed.state == "completed":
                if refreshed.response_status is None or refreshed.response_body is None:
                    raise RuntimeError("Completed idempotency operation has no response")
                return IdempotentResponse(
                    status_code=refreshed.response_status,
                    body=refreshed.response_body,
                    replayed=True,
                )
        raise IdempotencyInProgress("Matching operation is still in progress")
