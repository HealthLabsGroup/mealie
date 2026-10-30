"""Provider identity is independent of request credentials and ORM persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mealie.db.models.group.ai_providers import AIProvider, AIProviderSettings
from mealie.schema.group.ai_providers import AIProviderOut

_ConfigurationRows = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ResolvedCuratorProvider:
    group_id: UUID
    identity_id: UUID
    audit_provider_id: UUID | None
    name: str
    base_url: str | None
    model: str
    timeout: int
    request_headers: tuple[tuple[str, str], ...] = field(repr=False)
    request_params: tuple[tuple[str, str], ...] = field(repr=False)
    _credential: Callable[[], str] = field(repr=False, compare=False)
    _legacy_configuration: tuple[_ConfigurationRows, _ConfigurationRows] | None = field(default=None, repr=False)

    def __reduce__(self):
        raise TypeError("runtime provider cannot be serialized")

    def transport(self) -> AIProviderOut:
        try:
            key = self._credential()
        except Exception:
            # Raise outside the handler so a secret-bearing source exception is not chained.
            key = ""
        if not key or not key.strip():
            raise ValueError("session provider credential unavailable")
        return AIProviderOut(
            id=self.identity_id,
            name=self.name,
            base_url=self.base_url,
            model=self.model,
            timeout=self.timeout,
            request_headers=dict(self.request_headers),
            request_params=dict(self.request_params),
            api_key=key,
        )

    def identity_context(self) -> dict[str, str | int | None | list[tuple[str, str]]]:
        """Keep persisted decision hashes stable; runtime identities hash actual configuration."""
        context: dict[str, str | int | None | list[tuple[str, str]]] = {
            "id": str(self.identity_id),
            "name": self.name,
            "baseUrl": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
        }
        if self._legacy_configuration is not None:
            headers, params = self._legacy_configuration
            context.update(headers=list(headers), params=list(params))
        else:
            config = json.dumps([self.request_headers, self.request_params], separators=(",", ":"))
            context["configurationSha256"] = hashlib.sha256(config.encode()).hexdigest()
        return context

    @classmethod
    def from_persisted(cls, provider: AIProvider) -> ResolvedCuratorProvider:
        return cls(
            group_id=provider.settings.group_id,
            identity_id=provider.id,
            audit_provider_id=provider.id,
            name=provider.name,
            base_url=provider.base_url,
            model=provider.model,
            timeout=provider.timeout,
            request_headers=tuple(sorted((item.key_name, item.value) for item in provider.request_headers)),
            request_params=tuple(sorted((item.key_name, item.value) for item in provider.request_params)),
            _credential=lambda: provider.api_key,
            _legacy_configuration=(
                tuple(
                    sorted(
                        (item.key_name or "", item.update_at.isoformat() if item.update_at else "")
                        for item in provider.request_headers
                    )
                ),
                tuple(
                    sorted(
                        (item.key_name or "", item.update_at.isoformat() if item.update_at else "")
                        for item in provider.request_params
                    )
                ),
            ),
        )


class CuratorProviderResolver(Protocol):
    def __call__(self, session: Session, group_id: UUID) -> ResolvedCuratorProvider | None: ...


def resolve_default_provider(session: Session, group_id: UUID) -> ResolvedCuratorProvider | None:
    settings = session.scalar(sa.select(AIProviderSettings).where(AIProviderSettings.group_id == group_id))
    if settings is None or settings.default_provider_id is None:
        return None
    provider = session.scalar(
        sa.select(AIProvider).where(
            AIProvider.id == settings.default_provider_id,
            AIProvider.settings_id == settings.id,
        )
    )
    return ResolvedCuratorProvider.from_persisted(provider) if provider is not None else None


@dataclass(frozen=True, slots=True, init=False)
class SessionProviderResolver:
    """An explicit per-group session allowlist; no fallback to stored credentials."""

    _providers: Mapping[UUID, ResolvedCuratorProvider] = field(repr=False)

    def __init__(
        self,
        *,
        identity_ids: Mapping[UUID, UUID],
        name: str,
        base_url: str,
        model: str,
        credential: Callable[[], str],
        timeout: int = 300,
        request_headers: Mapping[str, str] | None = None,
        request_params: Mapping[str, str] | None = None,
    ) -> None:
        if not identity_ids or not name.strip() or not base_url.strip() or not model.strip() or timeout < 0:
            raise ValueError("session provider identity and bounded groups are required")
        providers = {
            UUID(str(group_id)): ResolvedCuratorProvider(
                group_id=UUID(str(group_id)),
                identity_id=identity_id,
                audit_provider_id=None,
                name=name,
                base_url=base_url,
                model=model,
                timeout=timeout,
                request_headers=tuple(sorted((request_headers or {}).items())),
                request_params=tuple(sorted((request_params or {}).items())),
                _credential=credential,
            )
            for group_id, identity_id in identity_ids.items()
        }
        object.__setattr__(self, "_providers", MappingProxyType(providers))

    def __reduce__(self):
        raise TypeError("session provider resolver cannot be serialized")

    def __call__(self, session: Session, group_id: UUID) -> ResolvedCuratorProvider | None:
        provider = self._providers.get(UUID(str(group_id)))
        if provider is not None:
            provider.transport()
        return provider
