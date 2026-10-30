"""Frozen recipe-scoped processing using the ordinary durable jobs and claims.

The return value is a safe checkpoint, never execution authority. A resume always
reloads recipes, projections, job states and leases from the database.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, Self
from uuid import UUID

import httpx2
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator
from sqlalchemy.orm import Session

from mealie.db.db_setup import session_context
from mealie.db.models.healthlabs import HealthLabsNutrientSourceRelease
from mealie.db.models.healthlabs import HealthLabsRecipeMetadataEstimate as Job
from mealie.db.models.recipe import RecipeModel
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.recipe_metadata import MetadataPlants
from mealie.schema.recipe import Recipe
from mealie.services.healthlabs.nutrition_provider_resolution import CuratorProviderResolver, resolve_default_provider
from mealie.services.healthlabs.recipe_metadata_estimator import MetadataRequestLimits
from mealie.services.healthlabs.recipe_metadata_scheduler import RecipeMetadataScheduler
from mealie.services.healthlabs.recipe_metadata_service import (
    POLICY_VERSION,
    REQUIRED_NUTRIENTS,
    RecipeMetadataService,
    fingerprint,
)
from mealie.services.healthlabs.recipe_metadata_worker import RecipeMetadataReport, RecipeMetadataWorker

SessionFactory = Callable[[], AbstractContextManager[Session]]


class ManifestRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    recipe_id: UUID
    group_id: UUID
    scope: Literal["personal", "catalogue"] | None = None
    revision_id: UUID | None = None
    authoring_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RecipeManifest(RootModel[tuple[ManifestRecipe, ...]]):
    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def unique_recipes(self) -> Self:
        if not self.root or len(self.root) > 100_000:
            raise ValueError("manifest requires 1..100000 recipes")
        if len({row.recipe_id for row in self.root}) != len(self.root):
            raise ValueError("duplicate manifest recipe")
        return self

    def authorizes(self, session: Session, recipe_id: UUID) -> bool:
        entry = next((row for row in self.root if row.recipe_id == recipe_id), None)
        if entry is None:
            return False
        actual = session.execute(
            sa.select(RecipeModel.group_id, RecipeModel.healthlabs_scope).where(RecipeModel.id == recipe_id)
        ).first()
        if (
            actual is None
            or actual.group_id != entry.group_id
            or (entry.scope is not None and actual.healthlabs_scope != entry.scope)
        ):
            return False
        if entry.revision_id or entry.authoring_fingerprint or entry.input_fingerprint:
            return _load(session, entry)[1] == "current"
        return True

    @property
    def sha256(self) -> str:
        return fingerprint(sorted((r.model_dump(mode="json") for r in self.root), key=lambda r: r["recipe_id"]))

    @property
    def groups(self) -> frozenset[UUID]:
        return frozenset(r.group_id for r in self.root)

    @property
    def recipes(self) -> frozenset[UUID]:
        return frozenset(r.recipe_id for r in self.root)


def _load(session: Session, entry: ManifestRecipe) -> tuple[Recipe | None, str]:
    actual = session.execute(
        sa.select(RecipeModel.group_id, RecipeModel.healthlabs_scope).where(RecipeModel.id == entry.recipe_id)
    ).first()
    if actual is None:
        return None, "missing"
    if actual.group_id != entry.group_id or (entry.scope is not None and actual.healthlabs_scope != entry.scope):
        return None, "scope_moved"
    recipe = AllRepositories(session, group_id=entry.group_id, household_id=None).recipes.get_one(entry.recipe_id, "id")
    if recipe is None:
        return None, "missing"
    if entry.scope is not None and recipe.healthlabs_scope != entry.scope:
        return None, "scope_moved"
    if entry.revision_id is not None:
        from mealie.services.healthlabs.revision_service import RecipeRevisionService

        revision = RecipeRevisionService(session).latest(entry.recipe_id)
        if revision is None or revision.id != entry.revision_id:
            return recipe, "changed"
    if entry.authoring_fingerprint is not None or entry.input_fingerprint is not None:
        inputs = RecipeMetadataService(session).inputs(recipe)
        if any(
            expected is not None and expected != getattr(inputs, key)
            for key, expected in (
                ("authoring_fingerprint", entry.authoring_fingerprint),
                ("input_fingerprint", entry.input_fingerprint),
            )
        ):
            return recipe, "changed"
    return recipe, "current"


def _plant_quality(plants: MetadataPlants) -> str:
    if plants.status != "complete":
        return plants.status
    return "estimated" if plants.provenance_ids else "recorded"


def coverage(manifest: RecipeManifest, session_factory: SessionFactory = session_context) -> dict[str, Any]:
    nutrients: dict[str, Counter[str]] = {key: Counter() for key in (*REQUIRED_NUTRIENTS, "water_ml")}
    general: dict[str, Counter[str]] = {
        key: Counter()
        for key in ("servings", "total_minutes", "prep_minutes", "active_minutes", "effort", "plants", "source")
    }
    rows = []
    plant_quality: Counter[str] = Counter()
    source_ids: set[UUID] = set()
    source_versions = {}
    complete = 0
    with session_factory() as session:
        service = RecipeMetadataService(session)
        for entry in manifest.root:
            row: dict[str, Any] = {"recipe_id": str(entry.recipe_id), "group_id": str(entry.group_id)}
            recipe, status = _load(session, entry)
            row["status"] = status
            if recipe is None:
                plant_quality["unavailable"] += 1
                for counts in (*nutrients.values(), *general.values()):
                    counts["unavailable"] += 1
                rows.append(row)
                continue
            inputs = service.inputs(recipe)
            row.update(input_fingerprint=inputs.input_fingerprint, authoring_fingerprint=inputs.authoring_fingerprint)
            if any(
                expected is not None and expected != getattr(inputs, key)
                for key, expected in (
                    ("authoring_fingerprint", entry.authoring_fingerprint),
                    ("input_fingerprint", entry.input_fingerprint),
                )
            ):
                row["status"] = "changed"
            nutrition, metadata = service.project(recipe)
            plant_quality[_plant_quality(metadata.plants)] += 1
            missing: list[str] = []
            for key, counts in nutrients.items():
                field = getattr(nutrition.per_serving, key)
                counts[field.quality] += 1
                if key in REQUIRED_NUTRIENTS and field.value is None:
                    missing.append(key)
            for key, counts in general.items():
                field = getattr(metadata, key)
                quality = (
                    field.status if key == "plants" else field.attribution_status if key == "source" else field.quality
                )
                counts[quality] += 1
                if (key == "plants" and field.status != "complete") or (
                    key not in {"plants", "source"} and field.value is None
                ):
                    missing.append(key)
            row["missing_fields"] = missing
            row["ingredient_reference_ids"] = [str(f.reference_id) for f in inputs.ingredients]
            row["missing_verified_ingredient_refs"] = {
                key: [str(ref) for ref in getattr(nutrition.per_serving, key).missing_verified_ingredient_refs]
                for key in nutrients
            }
            row["source_release_ids"] = sorted(
                {str(f.accepted_source_release_id) for f in inputs.ingredients if f.accepted_source_release_id}
                | {p.source_release_id for p in nutrition.provenance if p.source_release_id}
            )
            source_ids.update(UUID(value) for value in row["source_release_ids"])
            jobs = list(
                session.scalars(
                    sa.select(Job).where(
                        Job.recipe_id == entry.recipe_id,
                        Job.group_id == entry.group_id,
                        Job.input_fingerprint == inputs.input_fingerprint,
                        Job.policy_version == POLICY_VERSION,
                    )
                )
            )
            row["jobs"] = [
                {
                    "id": str(j.id),
                    "state": j.state,
                    "attempts": j.attempt_count,
                    "reason": j.last_error_code,
                    "provider": j.provider_name,
                    "model": j.provider_model,
                    "usage": j.usage,
                    "next_attempt_at": j.next_attempt_at.isoformat(),
                    "lease_expires_at": j.lease_expires_at.isoformat() if j.lease_expires_at else None,
                }
                for j in jobs
            ]
            complete += int(not missing and row["status"] == "current")
            rows.append(row)
        for release in session.scalars(
            sa.select(HealthLabsNutrientSourceRelease).where(HealthLabsNutrientSourceRelease.id.in_(source_ids))
        ):
            source_versions[str(release.id)] = {
                "provider": release.provider,
                "version": release.version,
                "sha256": release.sha256,
            }
        session.rollback()
    return {
        "manifest_sha256": manifest.sha256,
        "policy_version": POLICY_VERSION,
        "operator_version": 1,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_versions": source_versions,
        "plant_quality": dict(plant_quality),
        "denominator": len(manifest.root),
        "complete": complete,
        "nutrients": {k: dict(v) for k, v in nutrients.items()},
        "general": {k: dict(v) for k, v in general.items()},
        "recipes": rows,
    }


def _validate_provider(
    manifest: RecipeManifest, resolver: CuratorProviderResolver | None, session_factory: SessionFactory
) -> list[dict[str, Any]]:
    identities = []
    with session_factory() as session:
        for group_id in sorted(manifest.groups, key=str):
            resolved = (resolver or resolve_default_provider)(session, group_id)
            if resolved is None:
                if resolver is not None:
                    raise ValueError("session provider unavailable for manifest group")
                continue
            if resolved.group_id != group_id:
                raise ValueError("provider scope mismatch")
            if resolver is not None:
                resolved.transport()
            identities.append({"group_id": str(group_id), **resolved.identity_context()})
    return identities


async def _schedule_manifest(
    manifest: RecipeManifest, session_factory: SessionFactory, should_stop: Callable[[], bool]
) -> list[UUID]:
    ids: list[UUID] = []
    with session_factory() as session:
        scheduler = RecipeMetadataScheduler(session)
        for index, entry in enumerate(manifest.root):
            recipe, status = _load(session, entry)
            if recipe is None or status != "current":
                raise ValueError("manifest scope changed")
            work_id = scheduler.schedule(recipe)
            if work_id is not None:
                ids.append(work_id)
            if (index + 1) % 20 == 0:
                session.commit()
                await asyncio.sleep(0)
            if should_stop():
                break
        session.commit()
    return ids


async def run_manifest(
    manifest: RecipeManifest,
    *,
    apply: bool = False,
    provider_resolver: CuratorProviderResolver | None = None,
    session_factory: SessionFactory = session_context,
    checkpoint: dict[str, Any] | None = None,
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
    limits: MetadataRequestLimits | None = None,
    concurrency: int = 8,
    max_jobs: int = 100_000,
    max_attempts: int = 3,
    corrections: int = 1,
    should_stop: Callable[[], bool] = lambda: False,
    http_client: httpx2.AsyncClient | None = None,
) -> dict[str, Any]:
    if not 1 <= concurrency <= 32 or not 1 <= max_jobs <= 100_000:
        raise ValueError("operator bounds: concurrency 1..32, jobs 1..100000")
    if checkpoint is not None and checkpoint.get("manifest_sha256") != manifest.sha256:
        raise ValueError("checkpoint manifest mismatch")
    result = coverage(manifest, session_factory)
    limits = limits or MetadataRequestLimits()
    result.update(requests=0, reserved_microusd=0, claimed=0, halt_reason=None)
    if not apply:
        return result
    if should_stop():
        result["halt_reason"] = "operator_stopped"
        return result
    if any(r["status"] != "current" for r in result["recipes"]):
        raise ValueError("manifest scope or pinned fingerprint changed")
    halt: str | None = None
    claimed = 0
    identities = _validate_provider(manifest, provider_resolver, session_factory)
    ids = await _schedule_manifest(manifest, session_factory, should_stop)
    pending: set[asyncio.Task[RecipeMetadataReport]] = set()
    iterator = iter(ids)
    exhausted = False

    async def execute(work_id: UUID) -> RecipeMetadataReport:
        with session_factory() as session:
            worker = RecipeMetadataWorker(
                session,
                provider_resolver=provider_resolver,
                group_ids=manifest.groups,
                recipe_ids=manifest.recipes,
                recipe_guard=manifest.authorizes,
                limits=limits,
                should_stop=lambda: should_stop() or halt is not None,
                http_client=http_client,
                max_attempts=max_attempts,
                corrections=corrections,
            )
            return await worker.run_work(work_id)

    async def drain() -> None:
        nonlocal halt, claimed, exhausted
        while pending or not exhausted:
            while (
                not exhausted
                and halt is None
                and not should_stop()
                and len(pending) < min(concurrency, max_jobs - claimed)
            ):
                if limits.requests >= limits.max_requests:
                    halt = "operator_request_limit"
                    break
                try:
                    work_id = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(asyncio.create_task(execute(work_id)))
            if not pending:
                break
            finished, remaining = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            pending.clear()
            pending.update(remaining)
            for task in finished:
                report = task.result()
                claimed += report.claimed
                if report.scope_changed:
                    halt = halt or "manifest_changed"
                if report.failed:
                    halt = halt or "provider_failure"
                if report.local_limit_reached or report.stopped or report.provider_exhausted:
                    halt = halt or report.limit_reason or "global_daily_budget"
            if claimed >= max_jobs:
                halt = halt or "operator_job_limit"
            if on_checkpoint:
                snapshot: dict[str, Any] = {
                    "manifest_sha256": manifest.sha256,
                    "policy_version": POLICY_VERSION,
                    "in_progress": True,
                    "scheduled_work_ids": [str(work_id) for work_id in ids],
                }
                snapshot.update(
                    requests=limits.requests,
                    reserved_microusd=limits.reserved_microusd,
                    claimed=claimed,
                    halt_reason=halt,
                )
                on_checkpoint(snapshot)

    try:
        if http_client is None:
            async with httpx2.AsyncClient(
                limits=httpx2.Limits(max_connections=concurrency), follow_redirects=True
            ) as client:
                http_client = client
                await drain()
        else:
            await drain()
    finally:
        # Cancellation leaves reservations and leases conservative; drain live
        # calls before releasing their Session/client resources on ordinary stop.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    result = coverage(manifest, session_factory)
    result.update(
        requests=limits.requests,
        reserved_microusd=limits.reserved_microusd,
        claimed=claimed,
        execution_providers=identities,
        halt_reason="operator_stopped" if should_stop() else halt,
    )
    if on_checkpoint:
        on_checkpoint(result)
    return result


def retry_terminal(
    manifest: RecipeManifest,
    *,
    work_id: UUID,
    expected_fingerprint: str,
    expected_state: str,
    expected_reason: str,
    operator_reason: str,
    session_factory: SessionFactory = session_context,
) -> bool:
    """An explicit one-row CAS after the operator resolved the recorded cause."""
    import re

    from mealie.db.models._model_utils.datetime import get_utc_now

    if (
        expected_state not in {"failed", "rejected"}
        or not expected_reason
        or re.fullmatch(r"[a-z][a-z0-9_]{2,47}", operator_reason) is None
    ):
        raise ValueError("terminal retry requires expected state/reason and bounded operator reason")
    with session_factory() as session:
        row = session.scalar(
            sa.select(Job).where(
                Job.id == work_id, Job.group_id.in_(manifest.groups), Job.recipe_id.in_(manifest.recipes)
            )
        )
        if row is None:
            return False
        entry = next((r for r in manifest.root if r.recipe_id == row.recipe_id and r.group_id == row.group_id), None)
        if entry is None:
            return False
        recipe, status = _load(session, entry)
        if (
            recipe is None
            or status != "current"
            or RecipeMetadataService(session).inputs(recipe).input_fingerprint != expected_fingerprint
        ):
            return False
        changed = (
            session.execute(
                sa.update(Job)
                .where(
                    Job.id == work_id,
                    Job.input_fingerprint == expected_fingerprint,
                    Job.state == expected_state,
                    Job.last_error_code == expected_reason,
                    Job.policy_version == POLICY_VERSION,
                )
                .values(
                    state="pending",
                    attempt_count=0,
                    next_attempt_at=get_utc_now(),
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code=f"operator_retry:{operator_reason}",
                )
            ).rowcount
            == 1
        )
        session.commit()
        return changed


def _source_receipt(session: Session, work_id: UUID) -> dict[str, Any]:
    from mealie.db.models.healthlabs import HealthLabsNutritionCuratorDecision as Decision
    from mealie.db.models.healthlabs import HealthLabsNutritionWork as Work

    work = session.get(Work, work_id)
    receipt: dict[str, Any] = {"work_id": str(work_id)}
    if work is not None:
        receipt.update(kind=work.kind, target_id=work.target_id, input_fingerprint=work.input_sha256, state=work.state)
    decision = session.execute(
        sa.select(
            Decision.id,
            Decision.state,
            Decision.curator_sha256,
            Decision.reason_code,
            Decision.attempt_count,
            Decision.next_attempt_at,
            Decision.provider_name,
            Decision.provider_model,
            Decision.usage,
        )
        .where(Decision.work_id == work_id)
        .order_by(Decision.created_at.desc(), Decision.id)
        .limit(1)
    ).first()
    if decision is not None:
        receipt["decision"] = {
            "id": str(decision.id),
            "state": decision.state,
            "fingerprint": decision.curator_sha256,
            "reason": decision.reason_code,
            "attempts": decision.attempt_count,
            "next_attempt_at": decision.next_attempt_at.isoformat(),
            "provider": decision.provider_name,
            "model": decision.provider_model,
            "usage": decision.usage,
        }
    return receipt


async def run_source_manifest(
    manifest: RecipeManifest,
    *,
    apply: bool = False,
    provider_resolver: CuratorProviderResolver | None = None,
    session_factory: SessionFactory = session_context,
    limits: MetadataRequestLimits | None = None,
    max_work: int = 400,
    reconciliation_rounds: int = 20,
    curator_kinds: tuple[str, ...] = ("food_match",),
    should_stop: Callable[[], bool] = lambda: False,
    http_client: httpx2.AsyncClient | None = None,
) -> dict[str, Any]:
    """Deterministic repair, then bounded existing candidate-only curator choices.

    No parser inference is run. Fingerprints retain the normal configured
    provider-enabled semantics. Terminal curator decisions are never cleared.
    """
    from mealie.core.config import get_app_settings
    from mealie.db.models.healthlabs import HealthLabsNutritionWork as Work
    from mealie.db.models.recipe import RecipeIngredientModel
    from mealie.services.healthlabs.nutrition_curator_worker import NutritionCuratorWorker
    from mealie.services.healthlabs.nutrition_worker import NutritionReconciliationWorker

    if not 1 <= max_work <= 100_000 or not 0 <= reconciliation_rounds <= 1000:
        raise ValueError("invalid source work/round bounds")
    if not curator_kinds or set(curator_kinds) - {"food_match", "ingredient_structure", "portion_resolution"}:
        raise ValueError("invalid source curator kinds")
    if apply and http_client is None:
        async with httpx2.AsyncClient(limits=httpx2.Limits(max_connections=1), follow_redirects=True) as client:
            return await run_source_manifest(
                manifest,
                apply=True,
                provider_resolver=provider_resolver,
                session_factory=session_factory,
                limits=limits,
                max_work=max_work,
                reconciliation_rounds=reconciliation_rounds,
                curator_kinds=curator_kinds,
                should_stop=should_stop,
                http_client=client,
            )
    initial = coverage(manifest, session_factory)
    if apply and any(r["status"] != "current" for r in initial["recipes"]):
        raise ValueError("manifest source scope or pinned fingerprint changed")
    limits = limits or MetadataRequestLimits()
    totals: dict[str, Any] = {
        "inspected": 0,
        "claimed": 0,
        "approved": 0,
        "insufficient_evidence": 0,
        "failed": 0,
        "reconciled": 0,
        "provider_errors": 0,
        "work": [],
        "halt_reason": None,
    }

    def targets(session: Session) -> dict[str, frozenset[str]]:
        # Validate only IDs/scopes in one query. Full authoring is loaded only
        # for explicitly pinned entries; no unrelated recipe text is read.
        scopes = {
            r.id: (r.group_id, r.healthlabs_scope)
            for r in session.execute(
                sa.select(RecipeModel.id, RecipeModel.group_id, RecipeModel.healthlabs_scope).where(
                    RecipeModel.id.in_(manifest.recipes)
                )
            )
        }
        for entry in manifest.root:
            current = scopes.get(entry.recipe_id)
            if (
                current is None
                or current[0] != entry.group_id
                or (entry.scope is not None and current[1] != entry.scope)
            ):
                raise ValueError("manifest source scope changed")
            if entry.revision_id or entry.authoring_fingerprint or entry.input_fingerprint:
                _, status = _load(session, entry)
                if status != "current":
                    raise ValueError("manifest source pin changed")
        ingredients = list(
            session.execute(
                sa.select(
                    RecipeIngredientModel.id,
                    RecipeIngredientModel.food_id,
                    RecipeIngredientModel.unit_id,
                    RecipeIngredientModel.quantity,
                )
                .join(RecipeModel, RecipeModel.id == RecipeIngredientModel.recipe_id)
                .where(RecipeModel.id.in_(manifest.recipes), RecipeModel.group_id.in_(manifest.groups))
            )
        )
        return {
            "ingredient_structure": frozenset(
                str(r.id) for r in ingredients if r.food_id is None and r.unit_id is None and r.quantity in (None, 0)
            ),
            "portion_resolution": frozenset(
                str(r.id)
                for r in ingredients
                if r.food_id is not None and r.unit_id is not None and r.quantity is not None and r.quantity > 0
            ),
            "food_match": frozenset(str(r.food_id) for r in ingredients if r.food_id),
        }

    with session_factory() as session:
        allowed = targets(session)
        totals["targets"] = {key: len(ids) for key, ids in allowed.items()}
    if not apply:
        initial.update(source=totals, requests=0, reserved_microusd=0)
        return initial
    if should_stop():
        totals["halt_reason"] = "operator_stopped"
        initial.update(source=totals, requests=0, reserved_microusd=0)
        return initial
    identities = _validate_provider(manifest, provider_resolver, session_factory)
    settings = get_app_settings()
    for _ in range(reconciliation_rounds):
        processed = 0
        for group in sorted(manifest.groups, key=str):
            if should_stop():
                break
            with session_factory() as session:
                reconciliation = await NutritionReconciliationWorker(
                    session,
                    group_id=group,
                    target_ids=targets(session),
                    recipe_ids=manifest.recipes,
                    recipe_guard=manifest.authorizes,
                    deterministic_only=True,
                    batch_size=100,
                    should_stop=should_stop,
                    provider_daily_request_limit=settings.HEALTHLABS_NUTRITION_PROVIDER_DAILY_REQUEST_LIMIT,
                    provider_daily_budget_usd=settings.HEALTHLABS_NUTRITION_PROVIDER_DAILY_BUDGET_USD,
                    provider_input_usd_per_million_tokens=settings.HEALTHLABS_NUTRITION_PROVIDER_INPUT_USD_PER_MILLION_TOKENS,
                    provider_output_usd_per_million_tokens=settings.HEALTHLABS_NUTRITION_PROVIDER_OUTPUT_USD_PER_MILLION_TOKENS,
                ).run()
                processed += reconciliation.processed
        totals["reconciled"] += processed
        if should_stop() or not processed:
            break
    with session_factory() as session:
        allowed = targets(session)
        totals["final_targets"] = {key: len(ids) for key, ids in allowed.items()}
        target_filter = sa.or_(
            *(sa.and_(Work.kind == kind, Work.target_id.in_(allowed[kind])) for kind in curator_kinds)
        )
        work_rows = list(
            session.execute(
                sa.select(Work.id, Work.kind, Work.target_id).where(Work.group_id.in_(manifest.groups), target_filter)
            )
        )
        impact = dict(
            session.execute(
                sa.select(RecipeIngredientModel.food_id, sa.func.count())
                .where(
                    RecipeIngredientModel.recipe_id.in_(manifest.recipes), RecipeIngredientModel.food_id.is_not(None)
                )
                .group_by(RecipeIngredientModel.food_id)
            ).all()
        )
        priority = {"food_match": 0, "portion_resolution": 1, "ingredient_structure": 2}
        ids = [
            r.id
            for r in sorted(
                work_rows,
                key=lambda r: (
                    priority[r.kind],
                    -impact.get(UUID(r.target_id), 0) if r.kind == "food_match" else 0,
                    str(r.id),
                ),
            )
        ]
    for work_id in ids:
        if should_stop() or totals["claimed"] >= max_work:
            totals["halt_reason"] = "operator_stopped" if should_stop() else "operator_job_limit"
            break
        if limits.requests >= limits.max_requests:
            totals["halt_reason"] = "operator_request_limit"
            break
        with session_factory() as session:
            # Revalidate live target ownership before EACH dispatch; work IDs
            # alone cannot authorize a target moved outside the frozen recipes.
            from mealie.services.healthlabs.recipe_metadata_source_refresh import source_work_recipe_scope

            work = session.scalar(
                sa.select(Work).where(
                    Work.id == work_id, Work.group_id.in_(manifest.groups), source_work_recipe_scope(manifest.recipes)
                )
            )
            if work is None or work.target_id not in allowed.get(work.kind, frozenset()):
                continue
            worker = NutritionCuratorWorker(
                session,
                provider_resolver=provider_resolver,
                group_ids=manifest.groups,
                work_ids={work_id},
                recipe_ids=manifest.recipes,
                recipe_guard=manifest.authorizes,
                request_limits=limits,
                sdk_max_retries=0,
                should_stop=should_stop,
                http_client=http_client,
            )
            totals["inspected"] += 1
            report = await worker.run_work(work_id)
            totals["provider_errors"] += int(report.provider_error)
            totals["work"].append(_source_receipt(session, work_id))
            for key in ("claimed", "approved", "insufficient_evidence", "failed"):
                totals[key] += getattr(report, key)
            if report.scope_changed:
                totals["halt_reason"] = "manifest_changed"
                break
            if report.throttled or report.transport_error:
                totals["halt_reason"] = "provider_throttled" if report.throttled else "provider_transport_error"
                break
            if report.provider_exhausted or report.local_limit_reached or report.stopped:
                totals["halt_reason"] = report.limit_reason or "global_daily_budget"
                break
        await asyncio.sleep(0)
    result = coverage(manifest, session_factory)
    result.update(
        source=totals,
        requests=limits.requests,
        reserved_microusd=limits.reserved_microusd,
        execution_providers=identities,
    )
    return result
