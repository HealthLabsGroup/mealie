import ipaddress
import socket

import httpx
import pytest

from mealie.pkgs import safehttp
from mealie.pkgs.safehttp import transport as safehttp_transport
from mealie.pkgs.safehttp.transport import AsyncSafeTransport, InvalidDomainError
from mealie.services.healthlabs.import_draft_service import IMPORT_MAX_BYTES, IMPORT_MAX_REDIRECTS, ImportDraftService


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fe80::1"],
)
def test_import_blocks_loopback_private_link_local_and_metadata(address: str) -> None:
    assert safehttp.is_blocked_ip(ipaddress.ip_address(address))
    with pytest.raises(InvalidDomainError):
        AsyncSafeTransport()._validate(
            httpx.Request("GET", f"http://[{address}]/" if ":" in address else f"http://{address}/")
        )


def test_import_pins_one_dns_resolution_to_prevent_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def resolve(host, port, *args, **kwargs):
        nonlocal calls
        calls += 1
        address = "93.184.216.34" if calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(safehttp_transport.socket, "getaddrinfo", resolve)
    transport = AsyncSafeTransport()
    pins = transport._validate(httpx.Request("GET", "https://recipes.example/path"))
    transport._apply_pin(pins)

    assert calls == 1
    assert transport._session.curl_options
    assert pins == ["recipes.example:443:93.184.216.34"]


@pytest.mark.asyncio
async def test_import_fetch_uses_safehttp_byte_and_redirect_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fetch(url: str, **kwargs):
        captured.update(kwargs)
        return safehttp.FetchResult(
            content=b"<html></html>",
            status_code=200,
            url=url,
            headers=httpx.Headers({"content-type": "text/html"}),
            encoding="utf-8",
        )

    monkeypatch.setattr(safehttp, "resilient_fetch", fetch)
    result = await ImportDraftService.fetch_document("https://recipes.example/test")

    assert result.content == b"<html></html>"
    assert captured["max_bytes"] == IMPORT_MAX_BYTES
    assert captured["max_redirects"] == IMPORT_MAX_REDIRECTS
    assert captured["allow_flaresolverr"] is False
