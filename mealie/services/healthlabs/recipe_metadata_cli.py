"""Dry-run by default. Session credentials are read only from stdin on --apply."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mealie.services.healthlabs.nutrition_provider_resolution import SessionProviderResolver
from mealie.services.healthlabs.recipe_metadata_estimator import MetadataRequestLimits
from mealie.services.healthlabs.recipe_metadata_operator import (
    RecipeManifest,
    retry_terminal,
    run_manifest,
    run_source_manifest,
)


class ProviderIdentities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    identity_ids: dict[UUID, UUID] = Field(min_length=1)
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=255)
    timeout: int = Field(default=300, ge=1, le=840)


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, repr=False, exclude=True)


def _code_version() -> str:
    try:
        return (
            subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
            or "installed-package"
        )
    except OSError:
        return "installed-package"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--stage", choices=("metadata", "sources"), default="metadata")
    parser.add_argument("--provider-identities", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-jobs", type=int, default=100_000)
    parser.add_argument("--max-requests", type=int, default=10_000)
    parser.add_argument("--max-cost-microusd", type=int, default=100_000_000)
    parser.add_argument("--max-requests-per-job", type=int, default=24)
    parser.add_argument("--max-input-tokens", type=int, default=196608)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--corrections", type=int, default=1)
    parser.add_argument("--source-max-work", type=int, default=400)
    parser.add_argument("--source-rounds", type=int, default=20)
    parser.add_argument(
        "--source-kinds",
        nargs="+",
        choices=("food_match", "ingredient_structure", "portion_resolution"),
        default=["food_match"],
    )
    parser.add_argument("--retry-work-id", type=UUID)
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--expected-state", choices=("failed", "rejected"))
    parser.add_argument("--expected-reason")
    parser.add_argument("--operator-reason")
    args = parser.parse_args()
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        manifest = RecipeManifest.model_validate_json(args.manifest.read_text())
        previous = json.loads(args.checkpoint.read_text()) if args.checkpoint and args.checkpoint.exists() else None
        if previous is not None and previous.get("manifest_sha256") != manifest.sha256:
            raise ValueError("manifest mismatch")
        identities = (
            ProviderIdentities.model_validate_json(args.provider_identities.read_text())
            if args.provider_identities
            else None
        )
        if identities is not None and not manifest.groups <= identities.identity_ids.keys():
            raise ValueError("provider identity scope missing")
        resolver = None
        # No stdin read on dry-run or on the ordinary stored-default path.
        if args.apply and identities is not None and not args.retry_work_id:
            credential = CredentialInput.model_validate_json(sys.stdin.read(65_536)).api_key
            resolver = SessionProviderResolver(**identities.model_dump(), credential=lambda: credential)
        limits = MetadataRequestLimits(
            max_requests=args.max_requests,
            max_cost_microusd=args.max_cost_microusd,
            max_requests_per_job=args.max_requests_per_job,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
        )
        version = _code_version()

        def save(value: dict[str, Any]) -> None:
            value["code_version"] = version
            if args.checkpoint:
                temporary = args.checkpoint.with_suffix(args.checkpoint.suffix + ".tmp")
                temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
                temporary.replace(args.checkpoint)

        if args.retry_work_id:
            if not args.apply or not all(
                (args.expected_fingerprint, args.expected_state, args.expected_reason, args.operator_reason)
            ):
                raise ValueError("explicit apply and complete terminal retry expectation required")
            changed = retry_terminal(
                manifest,
                work_id=args.retry_work_id,
                expected_fingerprint=args.expected_fingerprint,
                expected_state=args.expected_state,
                expected_reason=args.expected_reason,
                operator_reason=args.operator_reason,
            )
            result = {
                "manifest_sha256": manifest.sha256,
                "retried": changed,
                "work_id": str(args.retry_work_id),
                "expected_fingerprint": args.expected_fingerprint,
                "expected_state": args.expected_state,
                "expected_reason": args.expected_reason,
                "operator_reason": args.operator_reason,
            }
        elif args.stage == "sources":
            result = asyncio.run(
                run_source_manifest(
                    manifest,
                    apply=args.apply,
                    provider_resolver=resolver,
                    limits=limits,
                    max_work=args.source_max_work,
                    reconciliation_rounds=args.source_rounds,
                    curator_kinds=tuple(args.source_kinds),
                    should_stop=stop.is_set,
                )
            )
        else:
            result = asyncio.run(
                run_manifest(
                    manifest,
                    apply=args.apply,
                    provider_resolver=resolver,
                    checkpoint=previous,
                    on_checkpoint=save if args.apply else None,
                    limits=limits,
                    concurrency=args.concurrency,
                    max_jobs=args.max_jobs,
                    max_attempts=args.max_attempts,
                    corrections=args.corrections,
                    should_stop=stop.is_set,
                )
            )
        result["code_version"] = version
        result["operator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if args.apply:
            save(result)
    except OSError, ValueError, TypeError:
        parser.exit(2, "Invalid manifest, unavailable session credential, changed scope, or invalid bounds.\n")
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
