from collections.abc import Callable
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mealie.db.models.healthlabs import HealthLabsAccessAudit
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.healthlabs.identity import HealthLabsCapability


def test_cross_subject_plan_calls_require_bounded_reason_and_write_immutable_audits(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_plan_payload: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    subject, _ = healthlabs_provision()
    editor, editor_headers = healthlabs_provision()
    healthlabs_set_capabilities(editor["userId"], HealthLabsCapability.PLANS_WRITE_ALL)
    path = f"/api/healthlabs/v1/subjects/{subject['externalSubjectId']}/plan-publications"
    assert (
        api_client.post(
            path,
            json=healthlabs_plan_payload(),
            headers={**editor_headers, "Idempotency-Key": f"missing-reason-{uuid4()}"},
        ).status_code
        == 403
    )
    assert (
        api_client.post(
            path,
            json=healthlabs_plan_payload(),
            headers={
                **editor_headers,
                "Idempotency-Key": f"long-reason-{uuid4()}",
                "X-HealthLabs-Access-Reason": "x" * 501,
            },
        ).status_code
        == 422
    )
    reason = "Nutrition care plan"
    response = api_client.post(
        path,
        json=healthlabs_plan_payload(),
        headers={
            **editor_headers,
            "Idempotency-Key": f"publish-{uuid4()}",
            "X-HealthLabs-Access-Reason": reason,
        },
    )
    assert response.status_code == 201
    read = api_client.get(
        f"/api/healthlabs/v1/subjects/{subject['externalSubjectId']}/plans/current",
        headers={**editor_headers, "X-HealthLabs-Access-Reason": reason},
    )
    assert read.status_code == 200

    session = unfiltered_database.session
    audits = session.scalars(
        sa.select(HealthLabsAccessAudit)
        .where(HealthLabsAccessAudit.actor_user_id == editor["userId"])
        .order_by(HealthLabsAccessAudit.occurred_at)
    ).all()
    assert [audit.action for audit in audits] == ["plans.publish", "plans.read_cross_subject"]
    assert all(audit.reason == reason for audit in audits)
    assert all(audit.subject_external_id == subject["externalSubjectId"] for audit in audits)
    with pytest.raises(ValueError, match="immutable"):
        audits[0].reason = "rewritten"
        session.commit()
    session.rollback()


def test_plan_publication_replay_is_audited_for_the_replaying_editor(
    api_client: TestClient,
    healthlabs_provision: Callable,
    healthlabs_set_capabilities: Callable,
    healthlabs_plan_payload: Callable,
    unfiltered_database: AllRepositories,
) -> None:
    subject, _ = healthlabs_provision()
    first_editor, first_headers = healthlabs_provision()
    replaying_editor, replaying_headers = healthlabs_provision()
    healthlabs_set_capabilities(first_editor["userId"], HealthLabsCapability.PLANS_WRITE_ALL)
    healthlabs_set_capabilities(replaying_editor["userId"], HealthLabsCapability.PLANS_WRITE_ALL)
    path = f"/api/healthlabs/v1/subjects/{subject['externalSubjectId']}/plan-publications"
    payload = healthlabs_plan_payload()
    key = f"publish-{uuid4()}"
    first = api_client.post(
        path,
        json=payload,
        headers={
            **first_headers,
            "Idempotency-Key": key,
            "X-HealthLabs-Access-Reason": "Initial publication",
        },
    )
    replay = api_client.post(
        path,
        json=payload,
        headers={
            **replaying_headers,
            "Idempotency-Key": key,
            "X-HealthLabs-Access-Reason": "Verify publication",
        },
    )
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()

    audits = unfiltered_database.session.scalars(
        sa.select(HealthLabsAccessAudit)
        .where(HealthLabsAccessAudit.subject_external_id == subject["externalSubjectId"])
        .where(HealthLabsAccessAudit.action == "plans.publish")
        .order_by(HealthLabsAccessAudit.occurred_at)
    ).all()
    assert [(str(audit.actor_user_id), audit.reason) for audit in audits] == [
        (first_editor["userId"], "Initial publication"),
        (replaying_editor["userId"], "Verify publication"),
    ]
