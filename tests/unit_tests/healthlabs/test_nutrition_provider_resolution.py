import json
import pickle
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from mealie.schema.group.ai_providers import AIProviderOut
from mealie.services.healthlabs.nutrition_provider_resolution import SessionProviderResolver


def _resolver(group_id, *, key=lambda: "fake-session-secret"):
    return SessionProviderResolver(
        identity_ids={group_id: uuid4()},
        name="oai-kensai-prod",
        base_url="https://azure.example/openai/v1",
        model="gpt-5.6-sol",
        credential=key,
        request_headers={"X-Client": "curator"},
    )


def test_session_identity_is_stable_scoped_immutable_and_secret_safe():
    group_id = uuid4()
    resolver = _resolver(group_id)
    provider = resolver(None, group_id)
    assert provider is not None
    assert resolver(None, uuid4()) is None
    assert resolver(None, group_id).identity_id == provider.identity_id
    assert provider.audit_provider_id is None
    assert provider.model == "gpt-5.6-sol"
    assert "fake-session-secret" not in repr(provider) + repr(resolver)
    assert "fake-session-secret" not in json.dumps(provider.identity_context())
    with pytest.raises(FrozenInstanceError):
        provider.model = "different"
    with pytest.raises(TypeError):
        pickle.dumps(provider)
    transport = provider.transport()
    assert transport.id == provider.identity_id
    assert transport.api_key == "fake-session-secret"
    assert transport.request_headers == {"X-Client": "curator"}
    assert "fake-session-secret" not in repr(transport) + transport.model_dump_json()


def test_missing_key_fails_closed_and_never_falls_back():
    group_id = uuid4()
    resolver = _resolver(group_id, key=lambda: "")
    with pytest.raises(ValueError, match="credential unavailable"):
        resolver(None, group_id)


def test_transport_rechecks_credential_without_freezing_a_secret_in_identity():
    group_id = uuid4()
    keys = ["fake-session-secret"]
    provider = _resolver(group_id, key=lambda: keys[0])(None, group_id)
    keys[0] = ""
    with pytest.raises(ValueError, match="credential unavailable"):
        provider.transport()


def test_provider_transport_repr_hides_key():
    provider = AIProviderOut(id=uuid4(), name="test", model="model", api_key="fake-session-secret")
    assert "fake-session-secret" not in repr(provider)


def test_credential_source_error_never_exposes_secret_in_exception():
    group_id = uuid4()

    def unavailable():
        raise RuntimeError("fake-session-secret")

    with pytest.raises(ValueError, match="credential unavailable") as error:
        _resolver(group_id, key=unavailable)(None, group_id)
    assert "fake-session-secret" not in str(error.value)
    assert error.value.__context__ is None


def test_runtime_identity_still_fingerprints_actual_configuration_without_credentials():
    from dataclasses import replace

    group_id = uuid4()
    provider = _resolver(group_id)(None, group_id)
    context = provider.identity_context()
    assert "configurationSha256" in context and "headers" not in context and "params" not in context
    assert replace(provider, _credential=lambda: "rotated-fake-key").identity_context() == context
    assert replace(provider, request_headers=(("X-Client", "changed"),)).identity_context() != context
    assert replace(provider, request_params=(("temperature", "0"),)).identity_context() != context
