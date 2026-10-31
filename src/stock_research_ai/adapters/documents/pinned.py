"""P2-1: resolved-IP connection pinning for untrusted document hosts.

The fetchers resolve a hop's host once, validate EVERY answer with `is_public_address`,
and pin exactly those addresses for that host. `PinnedNetworkBackend` never resolves a
name itself: it connects only to a pinned, validated IP (and re-checks it), so a DNS
answer that changes between validation and connect (rebinding) is never used. httpcore
still runs TLS with `server_hostname` = the URL's host and httpx still sends that host
in `Host`, so certificate verification is against the original name, never the IP.
"""

import ipaddress
from collections.abc import Iterable, MutableMapping

import httpcore
import httpx

#: NAT64 well-known prefix (RFC 6052): a gateway would forward to the embedded IPv4.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
#: Deprecated IPv4-compatible IPv6 (::a.b.c.d).
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in _NAT64 or address in _IPV4_COMPATIBLE:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_public_address(value: str) -> bool:
    """True only for a globally routable unicast address. Refuses loopback, private,
    link-local, multicast, unspecified, reserved and shared (CGNAT) space, and checks
    the IPv4 embedded in mapped, compatible, 6to4, Teredo and NAT64 IPv6 forms too."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [address]
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id:
            return False
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            candidates.append(embedded)
    return all(
        candidate.is_global
        and not (
            candidate.is_multicast
            or candidate.is_loopback
            or candidate.is_private
            or candidate.is_link_local
            or candidate.is_reserved
            or candidate.is_unspecified
        )
        for candidate in candidates
    )


def pin_key(url: str) -> str:
    """The host exactly as httpcore will hand it to `connect_tcp` (IDNA ASCII, lower
    case, IPv6 without brackets) - also the name the fetchers resolve."""
    try:
        return httpx.URL(url).raw_host.decode("ascii").lower()
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise ValueError("Unsupported host") from exc


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connects only to validated addresses pinned for the requested host."""

    def __init__(
        self,
        pins: MutableMapping[str, tuple[str, ...]],
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._pins = pins
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = self._pins.get(host.lower(), ())
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise httpcore.ConnectError("Destination address was not validated")
        failure: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        assert failure is not None
        raise failure

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx's own transport (certificate + hostname verification on, no proxies, no
    environment), with a connection pool whose only network access is the pinned
    backend. No keep-alive: every request opens a connection against the pins current
    for its hop, so a redirect is always re-validated and re-pinned before connecting."""

    def __init__(
        self,
        pins: MutableMapping[str, tuple[str, ...]],
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_keepalive_connections=0,
            network_backend=PinnedNetworkBackend(pins, inner),
        )


__all__ = ["PinnedNetworkBackend", "PinnedTransport", "is_public_address", "pin_key"]
