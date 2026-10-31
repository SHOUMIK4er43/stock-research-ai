"""P2-1: resolved-IP connection pinning (DNS rebinding). Offline: the real httpx/httpcore
stack runs over a fake socket layer that records which IP each connection went to, the
TLS server_hostname and the raw request bytes. DNS is a scripted fake."""

import ssl
from collections.abc import Iterable

import httpcore
import httpx
import pytest

from stock_research_ai.adapters.documents.evidence_fetcher import SafeEvidenceFetcher
from stock_research_ai.adapters.documents.pinned import (
    PinnedNetworkBackend,
    PinnedTransport,
    is_public_address,
    pin_key,
)
from stock_research_ai.adapters.documents.source import DocumentSource
from stock_research_ai.domain.documents import DocumentRequest
from stock_research_ai.domain.provenance import RetrievalStatus

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"

# Captured at import, before conftest's autouse offline guard replaces it.
_REAL_HANDLE = httpx.AsyncHTTPTransport.handle_async_request


@pytest.fixture(autouse=True)
def _pinned_transport_over_fake_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every PinnedTransport here runs over `_Sockets` (never a real socket; the
    socket-level offline guard stays in force), so its real request path may run."""
    monkeypatch.setattr(PinnedTransport, "handle_async_request", _REAL_HANDLE)


class _Stream(httpcore.AsyncNetworkStream):
    def __init__(self, socket: "_Sockets", address: str, response: bytes) -> None:
        self._socket, self.address, self._response = socket, address, response
        self.written = b""

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self._response = self._response[:max_bytes], self._response[max_bytes:]
        return chunk

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written += buffer
        self._socket.requests.append((self.address, self.written))

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self._socket.tls.append((self.address, server_hostname, ssl_context))
        return self


class _Sockets(httpcore.AsyncNetworkBackend):
    """The fake OS: records every connect target; `responses` maps an IP to the bytes
    its server sends back."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.connects: list[tuple[str, int]] = []
        self.tls: list[tuple[str, str | None, ssl.SSLContext]] = []
        self.requests: list[tuple[str, bytes]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append((host, port))
        if host not in self.responses:
            raise httpcore.ConnectError("unreachable")
        return _Stream(self, host, self.responses[host])


def _ok(body: bytes = b"%PDF-1.7 ok") -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def _redirect(location: str) -> bytes:
    return f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()


class _Dns:
    """Scripted resolver: each host has a queue of answers; the last answer repeats."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self.answers = answers
        self.queries: list[str] = []

    async def __call__(self, host: str) -> list[str]:
        self.queries.append(host)
        queue = self.answers[host]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _fetcher(dns: _Dns, sockets: _Sockets, monkeypatch: pytest.MonkeyPatch) -> SafeEvidenceFetcher:
    import stock_research_ai.adapters.documents.evidence_fetcher as module

    monkeypatch.setattr(module, "PinnedTransport", lambda pins: PinnedTransport(pins, sockets))
    return SafeEvidenceFetcher(lookup=dns)


async def _fetch(fetcher: SafeEvidenceFetcher, url: str):  # type: ignore[no-untyped-def]
    return await fetcher.fetch(url, max_bytes=10_000, timeout_seconds=5, max_redirects=3)


# --- address policy ---------------------------------------------------------------------------


@pytest.mark.parametrize("address", [PUBLIC_V4, "8.8.8.8", PUBLIC_V6, "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback v4
        "127.8.9.10",
        "::1",  # loopback v6
        "10.1.2.3",  # RFC1918
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # link-local / cloud metadata
        "fe80::1",
        "fe80::1%en0",
        "fd00::1",  # unique local
        "100.64.0.1",  # CGNAT shared space
        "0.0.0.0",
        "::",
        "224.0.0.1",  # multicast: is_global alone says True
        "239.255.255.250",
        "ff02::1",
        "ff0e::1",
        "240.0.0.1",  # reserved
        "255.255.255.255",
        "::ffff:127.0.0.1",  # IPv4-mapped bypasses
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "::127.0.0.1",  # IPv4-compatible: is_global alone says True
        "64:ff9b::7f00:1",  # NAT64 to 127.0.0.1: is_global alone says True
        "64:ff9b::a00:1",
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
        "not-an-ip",
        "",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not is_public_address(address)


def test_pin_key_matches_what_httpcore_connects_to() -> None:
    assert pin_key("https://Example.COM/a") == "example.com"
    assert pin_key("https://[2606:2800:220:1::1]/a") == "2606:2800:220:1::1"
    assert pin_key("https://bücher.example/a") == "xn--bcher-kva.example"


# --- the backend itself -----------------------------------------------------------------------


async def test_backend_never_connects_to_an_unpinned_host() -> None:
    sockets = _Sockets({PUBLIC_V4: _ok()})
    backend = PinnedNetworkBackend({}, sockets)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 443)
    assert sockets.connects == []


async def test_backend_refuses_a_non_public_pin_even_if_one_was_injected() -> None:
    sockets = _Sockets({"127.0.0.1": _ok()})
    backend = PinnedNetworkBackend({"example.com": (PUBLIC_V4, "127.0.0.1")}, sockets)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 443)
    assert sockets.connects == []


async def test_backend_refuses_unix_sockets() -> None:
    with pytest.raises(httpcore.ConnectError):
        await PinnedNetworkBackend({}, _Sockets({})).connect_unix_socket("/tmp/x.sock")


async def test_backend_tries_each_validated_address_in_order() -> None:
    sockets = _Sockets({PUBLIC_V6: _ok()})
    backend = PinnedNetworkBackend({"example.com": (PUBLIC_V4, PUBLIC_V6)}, sockets)
    stream = await backend.connect_tcp("example.com", 443)
    assert sockets.connects == [(PUBLIC_V4, 443), (PUBLIC_V6, 443)]
    assert isinstance(stream, _Stream) and stream.address == PUBLIC_V6


def test_transport_pool_uses_the_pinned_backend_with_verification_on() -> None:
    """Guards the httpx integration point: if an httpx upgrade stopped honouring the
    replaced pool, this fails rather than silently resolving DNS again."""
    transport = PinnedTransport({})
    pool = transport._pool
    assert isinstance(pool._network_backend, PinnedNetworkBackend)
    context = pool._ssl_context
    assert context is not None
    assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED


# --- end to end through SafeEvidenceFetcher --------------------------------------------------


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6])
async def test_public_ipv4_and_ipv6_connect_to_the_validated_ip_with_host_and_sni_intact(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dns = _Dns({"docs.example.com": [[address]]})
    sockets = _Sockets({address: _ok()})
    result = await _fetch(_fetcher(dns, sockets, monkeypatch), "https://docs.example.com/a.pdf")
    assert result.outcome == "OK" and result.content == b"%PDF-1.7 ok"
    assert sockets.connects == [(address, 443)]
    # TLS: SNI and certificate hostname check use the original name, never the IP.
    [(tls_ip, server_hostname, context)] = sockets.tls
    assert (tls_ip, server_hostname) == (address, "docs.example.com")
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    request = sockets.requests[-1][1]
    assert request.startswith(b"GET /a.pdf HTTP/1.1\r\n")
    assert b"\r\nHost: docs.example.com\r\n" in request


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "::1", "10.0.0.5", "192.168.0.10", "169.254.169.254", "fe80::1",
                "::ffff:127.0.0.1", "224.0.0.1"]
)  # fmt: skip
async def test_a_host_resolving_to_a_blocked_address_is_never_connected(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sockets = _Sockets({address: _ok()})
    result = await _fetch(
        _fetcher(_Dns({"evil.example.com": [[address]]}), sockets, monkeypatch),
        "https://evil.example.com/a.pdf",
    )
    assert (result.outcome, result.code) == ("UNSUPPORTED", "PRIVATE_DESTINATION")
    assert sockets.connects == []


async def test_one_private_answer_among_public_ones_blocks_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets = _Sockets({PUBLIC_V4: _ok(), "10.0.0.1": _ok()})
    dns = _Dns({"mixed.example.com": [[PUBLIC_V4, "10.0.0.1"]]})
    result = await _fetch(_fetcher(dns, sockets, monkeypatch), "https://mixed.example.com/a")
    assert result.code == "PRIVATE_DESTINATION" and sockets.connects == []


async def test_dns_rebinding_between_validation_and_connect_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first answer is public, every later answer is loopback. The connection must go
    to the validated public IP, and the name must be resolved exactly once."""
    dns = _Dns({"rebind.example.com": [[PUBLIC_V4], ["127.0.0.1"]]})
    sockets = _Sockets({PUBLIC_V4: _ok(), "127.0.0.1": _ok(b"%PDF-internal secret")})
    result = await _fetch(_fetcher(dns, sockets, monkeypatch), "https://rebind.example.com/x")
    assert result.outcome == "OK" and result.content == b"%PDF-1.7 ok"
    assert sockets.connects == [(PUBLIC_V4, 443)]
    assert dns.queries == ["rebind.example.com"]


async def test_redirect_public_to_a_host_resolving_private_is_revalidated_and_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns = _Dns({"a.example.com": [[PUBLIC_V4]], "internal.example.com": [["10.0.0.7"]]})
    sockets = _Sockets(
        {PUBLIC_V4: _redirect("https://internal.example.com/secret"), "10.0.0.7": _ok()}
    )
    result = await _fetch(_fetcher(dns, sockets, monkeypatch), "https://a.example.com/x")
    assert (result.outcome, result.code) == ("BLOCKED", "PRIVATE_DESTINATION")
    assert sockets.connects == [(PUBLIC_V4, 443)]
    assert dns.queries == ["a.example.com", "internal.example.com"]


@pytest.mark.parametrize(
    "location",
    ["https://127.0.0.1/x", "https://[::1]/x", "https://[::ffff:127.0.0.1]/x",
     "https://10.0.0.1/x", "https://localhost/x", "http://b.example.com/x"],
)  # fmt: skip
async def test_redirect_to_a_private_literal_or_downgrade_is_blocked_before_dns(
    location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dns = _Dns({"a.example.com": [[PUBLIC_V4]]})
    sockets = _Sockets({PUBLIC_V4: _redirect(location)})
    result = await _fetch(_fetcher(dns, sockets, monkeypatch), "https://a.example.com/x")
    assert (result.outcome, result.code) == ("BLOCKED", "UNSAFE_REDIRECT")
    assert sockets.connects == [(PUBLIC_V4, 443)] and dns.queries == ["a.example.com"]


async def test_redirect_to_another_public_host_is_repinned_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hop 2's host rebinds after its own validation; hop 2 still connects to the IP
    validated for hop 2, and hop 1's pin is not reused for it."""
    other = "93.184.216.99"
    dns = _Dns({"a.example.com": [[PUBLIC_V4]], "b.example.com": [[other], ["127.0.0.1"]]})
    sockets = _Sockets({PUBLIC_V4: _redirect("https://b.example.com/y"), other: _ok()})
    result = await _fetch(_fetcher(dns, sockets, monkeypatch), "https://a.example.com/x")
    assert result.outcome == "OK"
    assert sockets.connects == [(PUBLIC_V4, 443), (other, 443)]
    assert [hostname for _, hostname, _ in sockets.tls] == ["a.example.com", "b.example.com"]
    assert b"\r\nHost: b.example.com\r\n" in sockets.requests[-1][1]


# --- DocumentSource uses the same pinning -----------------------------------------------------


async def test_document_source_connects_only_to_the_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stock_research_ai.adapters.documents.source as module

    dns = _Dns({"ir.example.com": [[PUBLIC_V4], ["169.254.169.254"]]})
    sockets = _Sockets({PUBLIC_V4: _ok(), "169.254.169.254": _ok(b"%PDF-metadata")})
    monkeypatch.setattr(module, "PinnedTransport", lambda pins: PinnedTransport(pins, sockets))
    payload = await DocumentSource(lookup=dns).acquire(
        DocumentRequest(url="https://ir.example.com/annual.pdf", provider="company_ir")
    )
    assert payload.retrieval.status == RetrievalStatus.SUCCESS
    assert payload.content == b"%PDF-1.7 ok"
    assert sockets.connects == [(PUBLIC_V4, 443)]
    assert [hostname for _, hostname, _ in sockets.tls] == ["ir.example.com"]


async def test_document_source_redirect_to_private_resolution_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stock_research_ai.adapters.documents.source as module

    dns = _Dns({"ir.example.com": [[PUBLIC_V4]], "intra.example.com": [["192.168.1.20"]]})
    sockets = _Sockets(
        {PUBLIC_V4: _redirect("https://intra.example.com/a.pdf"), "192.168.1.20": _ok()}
    )
    monkeypatch.setattr(module, "PinnedTransport", lambda pins: PinnedTransport(pins, sockets))
    payload = await DocumentSource(lookup=dns).acquire(
        DocumentRequest(url="https://ir.example.com/annual.pdf", provider="company_ir")
    )
    assert payload.retrieval.status == RetrievalStatus.FAILED
    assert sockets.connects == [(PUBLIC_V4, 443)]


async def test_default_constructed_fetchers_use_the_pinned_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production wiring: no injected transport means the pinned transport is used."""
    built: list[PinnedTransport] = []
    import stock_research_ai.adapters.documents.evidence_fetcher as module

    def build(pins: dict[str, tuple[str, ...]]) -> PinnedTransport:
        built.append(PinnedTransport(pins, _Sockets({PUBLIC_V4: _ok()})))
        return built[-1]

    monkeypatch.setattr(module, "PinnedTransport", build)
    result = await _fetch(
        SafeEvidenceFetcher(lookup=_Dns({"a.example.com": [[PUBLIC_V4]]})),
        "https://a.example.com/x",
    )
    assert result.outcome == "OK" and len(built) == 1
    assert isinstance(httpx.AsyncClient(transport=built[0])._transport, PinnedTransport)
