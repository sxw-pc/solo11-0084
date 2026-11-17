"""Numeric loopback peers, resolution filtering, and pinned socket dialling."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

from .errors import SecurityBoundaryError

LOOPBACK_V4 = "127.0.0.1"
LOOPBACK_V6 = "::1"


class PeerMismatchError(SecurityBoundaryError):
    """A connected socket's peer is not the pinned numeric loopback address."""


def numeric_loopback(host: str) -> str | None:
    """Return the canonical numeric loopback address, or None if not literal loopback."""
    if not host or "%" in host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.version == 4 and address == ipaddress.IPv4Address(LOOPBACK_V4):
        return LOOPBACK_V4
    if address.version == 6 and address == ipaddress.IPv6Address(LOOPBACK_V6):
        return LOOPBACK_V6
    return None


def _canonical_loopback(address: str) -> str | None:
    """Canonicalise a loopback address, folding IPv4-mapped IPv6 forms."""
    text = str(address).split("%", 1)[0].strip().lower()
    if text.startswith("::ffff:") and "." in text:
        text = text[len("::ffff:") :]
    return numeric_loopback(text)


@dataclass(frozen=True)
class ResolvedPeer:
    """A numeric socket endpoint selected for a validated target."""

    family: int
    address: str
    port: int
    flowinfo: int = 0
    scope_id: int = 0

    @property
    def sockaddr(self) -> tuple:
        if self.family == socket.AF_INET6:
            return (self.address, self.port, self.flowinfo, self.scope_id)
        return (self.address, self.port)

    @property
    def is_loopback(self) -> bool:
        return numeric_loopback(self.address.split("%", 1)[0]) is not None

    @property
    def text(self) -> str:
        host = f"[{self.address}]" if self.family == socket.AF_INET6 else self.address
        return f"{host}:{self.port}"


def literal_peer(host: str, port: int) -> ResolvedPeer:
    canonical = numeric_loopback(host)
    if canonical is None:
        raise SecurityBoundaryError(f"not a numeric loopback address: {host!r}")
    family = socket.AF_INET6 if ":" in canonical else socket.AF_INET
    return ResolvedPeer(family, canonical, port)


def peer_from_sockaddr(family: int, sockaddr) -> ResolvedPeer | None:
    if not isinstance(sockaddr, tuple) or len(sockaddr) < 2:
        return None
    if family == socket.AF_INET:
        return ResolvedPeer(family, str(sockaddr[0]), int(sockaddr[1]))
    if family == socket.AF_INET6:
        flowinfo = int(sockaddr[2]) if len(sockaddr) > 2 else 0
        scope_id = int(sockaddr[3]) if len(sockaddr) > 3 else 0
        return ResolvedPeer(family, str(sockaddr[0]), int(sockaddr[1]), flowinfo, scope_id)
    return None


def endpoint_of(sock) -> tuple[str, int] | None:
    """Read the actual remote endpoint of a connected socket, if available."""
    try:
        raw = sock.getpeername()
    except (AttributeError, OSError):
        return None
    if not isinstance(raw, tuple) or len(raw) < 2:
        return None
    try:
        return str(raw[0]).split("%", 1)[0], int(raw[1])
    except (TypeError, ValueError):
        return None


def peer_matches_endpoint(peer: ResolvedPeer, endpoint: tuple[str, int] | None) -> bool:
    if endpoint is None or len(endpoint) < 2:
        return False
    expected = _canonical_loopback(peer.address)
    actual = _canonical_loopback(endpoint[0])
    return expected is not None and expected == actual and peer.port == endpoint[1]


class LoopbackConnector:
    """Resolves names and opens sockets only through numeric loopback addresses.

    ``resolve`` deliberately returns every resolver result; the client filters
    and audits them. ``connect`` refuses to hand a non-loopback endpoint to the
    kernel and verifies the connected socket's peer before returning it.
    """

    def __init__(self, resolver=None, socket_factory=None) -> None:
        self._resolver = resolver
        self._socket_factory = socket_factory
        self.last_resolution: list[ResolvedPeer] = []

    def resolve(self, host: str, port: int) -> list[ResolvedPeer]:
        resolver = self._resolver or socket.getaddrinfo
        infos = resolver(host, port, type=socket.SOCK_STREAM)
        peers: list[ResolvedPeer] = []
        for info in infos:
            family, socktype = info[0], info[1]
            if socktype not in {socket.SOCK_STREAM, 0}:
                continue
            peer = peer_from_sockaddr(family, info[4])
            if peer is not None:
                peers.append(peer)
        self.last_resolution = peers
        return peers

    def connect(self, peer: ResolvedPeer, timeout: float):
        if not peer.is_loopback:
            raise SecurityBoundaryError(
                f"refusing to connect to non-loopback address {peer.text}"
            )
        factory = self._socket_factory or socket.socket
        sock = factory(peer.family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(peer.sockaddr)
            if not peer_matches_endpoint(peer, endpoint_of(sock)):
                raise PeerMismatchError(
                    f"connected peer does not match pinned loopback address {peer.text}"
                )
        except BaseException:
            sock.close()
            raise
        return sock
