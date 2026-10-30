"""Bounded, opt-in curator session. The manifest contains identities, never credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mealie.db.db_setup import session_context
from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorWorker
from mealie.services.healthlabs.nutrition_provider_resolution import SessionProviderResolver


class SessionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_ids: dict[UUID, UUID] = Field(min_length=1)
    work_ids: list[UUID] = Field(min_length=1)
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout: int = Field(default=300, ge=0)

    def resolver(self, credential_env: str) -> SessionProviderResolver:
        return SessionProviderResolver(
            identity_ids=self.identity_ids,
            name=self.name,
            base_url=self.base_url,
            model=self.model,
            timeout=self.timeout,
            credential=lambda: os.environ.get(credential_env, ""),
        )


async def run_manifest(manifest: SessionManifest, *, credential_env: str, apply: bool, max_work: int) -> dict[str, int]:
    if max_work < 1:
        raise ValueError("max_work must be positive")
    resolver = manifest.resolver(credential_env)
    groups = set(manifest.identity_ids)
    with session_context() as session:
        worker = NutritionCuratorWorker(session, provider_resolver=resolver, group_ids=groups)
        due = set(worker.work_ids())
        work_ids = [work_id for work_id in dict.fromkeys(manifest.work_ids) if work_id in due]
        counts = {
            "eligible": min(len(work_ids), max_work),
            "claimed": 0,
            "approved": 0,
            "insufficient_evidence": 0,
            "failed": 0,
        }
        if not apply:
            return counts
        # Validate availability for every authorized group before claiming any item.
        for group_id in groups:
            resolver(session, group_id)
        for work_id in work_ids:
            report = await worker.run_work(work_id)
            for key in ("claimed", "approved", "insufficient_evidence", "failed"):
                counts[key] += getattr(report, key)
            if report.provider_exhausted or counts["claimed"] >= max_work:
                break
        return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--credential-env", default="MEALIE_SESSION_PROVIDER_KEY")
    parser.add_argument("--max-work", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        manifest = SessionManifest.model_validate_json(args.manifest.read_text())
        result = asyncio.run(
            run_manifest(manifest, credential_env=args.credential_env, apply=args.apply, max_work=args.max_work)
        )
    except OSError, ValueError:
        # Do not print validation input, environment values, or provider exception payloads.
        parser.exit(2, "Invalid session manifest, unavailable credential, or invalid work bound.\n")
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
