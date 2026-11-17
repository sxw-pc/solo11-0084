"""Loopback-only URL, address-contract, and bind-address enforcement."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit, urlunsplit

from .addresses import (
    LOOPBACK_V4,
    LOOPBACK_V6,
    LoopbackConnector,
    PeerMismatchError,
    ResolvedPeer,
    literal_peer,
    numeric_loopback,
    peer_matches_endpoint,
)
from .errors import SecurityBoundaryError

LOOPBACK_NAMES = frozenset({LOOPBACK_V4, "localhost", LOOPBACK_V6})

__all__ = [
    "AddressContract",
    "LOOPBACK_NAMES",
    "LOOPBACK_V4",
    "LOOPBACK_V6",
    "LoopbackConnector",
    "PeerMismatchError",
    "ResolvedPeer",
    "UrlPolicy",
    "ValidatedTarget",
    "is_loopback_host",
    "is_numeric_loopback",
    "literal_peer",
    "host_header_allowed",
    "numeric_loopback",
    "origin_of",
    "peer_matches_endpoint",
    "require_loopback_bind",
    "resolve_bind_address",
]


def is_numeric_loopback(host: str) -> bool:
    return numeric_loopback(host) is not None


def is_loopback_host(host: str) -> bool:
    normalized = (host or "").strip().lower().rstrip(".")
    return normalized == "localhost" or is_numeric_loopback(normalized)


def require_loopback_bind(host: str) -> str:
    normalized = (host or "").strip().lower()
    if not is_loopback_host(normalized):
        raise SecurityBoundaryError(f"refusing non-loopback bind address: {host!r}")
    return normalized


def resolve_bind_address(host: str, resolver=None) -> str:
    """Map a configuration bind host to a trusted numeric loopback address.

    Numeric loopback literals are used verbatim. The only permitted name,
    ``localhost``, is resolved but every non-loopback result is discarded; if
    no trusted candidate remains the bind is refused instead of falling back
    to the name.
    """
    normalized = require_loopback_bind(host)
    canonical = numeric_loopback(normalized)
    if canonical is not None:
        return canonical
    try:
        candidates = LoopbackConnector(resolver).resolve(normalized, 0)
    except (OSError, socket.gaierror) as exc:
        raise SecurityBoundaryError(
            f"cannot resolve loopback bind address {normalized!r}: {exc}"
        ) from exc
    allowed = [peer for peer in candidates if peer.is_loopback]
    if not allowed:
        raise SecurityBoundaryError(
            f"no trusted loopback address for bind host {normalized!r}"
        )
    allowed.sort(key=lambda peer: 0 if peer.family == socket.AF_INET else 1)
    return allowed[0].address


class AddressContract(str, Enum):
    """How the portal's configured host may be turned into a socket address."""

    LITERAL = "literal"
    PINNED_NAME = "pinned_name"

    @classmethod
    def parse(cls, value: "AddressContract | str") -> "AddressContract":
        if isinstance(value, AddressContract):
            return value
        try:
            return cls(str(value))
        except ValueError as exc:
            raise SecurityBoundaryError(
                f"unknown address contract: {value!r} (expected 'literal' or 'pinned_name')"
            ) from exc


def _origin_parts(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise SecurityBoundaryError("URL is empty or too long")
    if any(ord(char) < 0x21 or ord(char) == 0x7F or ord(char) > 0x7E for char in url):
        raise SecurityBoundaryError("URL contains spaces, controls, or non-ASCII text")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise SecurityBoundaryError(f"invalid URL: {url!r}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme != "http":
        raise SecurityBoundaryError(f"only plain HTTP to the local fixture is allowed: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise SecurityBoundaryError("URL userinfo is not permitted")
    host = (parts.hostname or "").lower().rstrip(".")
    if "%" in host:
        raise SecurityBoundaryError("IPv6 zone identifiers are not permitted")
    if not is_loopback_host(host):
        raise SecurityBoundaryError(f"non-loopback target refused: {host or '<empty>'!r}")
    if port is None:
        port = 80
    if not 1 <= port <= 65535:
        raise SecurityBoundaryError(f"invalid target port: {port}")
    return scheme, host, port


def origin_of(url: str) -> tuple[str, str, int]:
    return _origin_parts(url)


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    scheme: str
    host: str
    port: int
    request_target: str

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = (self.scheme == "http" and self.port == 80) or (
            self.scheme == "https" and self.port == 443
        )
        return host if default else f"{host}:{self.port}"


def host_header_allowed(
    raw_host: str, allowed_hosts: frozenset[str], allowed_ports: frozenset[int]
) -> bool:
    """Accept only a loopback Host whose port matches the configured portal port."""
    value = (raw_host or "").strip()
    if not value:
        return True
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return False
        host_name, suffix = value[1:end], value[end + 1 :]
        if suffix and not suffix.startswith(":"):
            return False
        port_text = suffix[1:] if suffix else ""
    elif value.count(":") == 1:
        host_name, port_text = value.split(":", 1)
    elif ":" in value:
        return False
    else:
        host_name, port_text = value, ""
    host_name = host_name.strip().lower()
    if not is_loopback_host(host_name):
        return False
    if port_text == "":
        port = 80
    elif port_text.isdigit() and 1 <= int(port_text) <= 65535:
        port = int(port_text)
    else:
        return False
    return host_name in allowed_hosts and port in allowed_ports


class UrlPolicy:
    """Permits only the exact configured local portal origin and address contract."""

    def __init__(
        self,
        base_url: str,
        *,
        contract: AddressContract | str = AddressContract.LITERAL,
    ) -> None:
        self.contract = AddressContract.parse(contract)
        self.origin = origin_of(base_url)
        self.base_url = base_url.rstrip("/")
        self._check_contract_host(self.origin[1])

    def _check_contract_host(self, host: str) -> None:
        if self.contract is AddressContract.LITERAL:
            if numeric_loopback(host) is None:
                reason = (
                    "literal address contract refuses the host name 'localhost'; "
                    "configure a numeric loopback IP"
                    if host == "localhost"
                    else f"literal address contract refuses non-numeric host {host!r}"
                )
                raise SecurityBoundaryError(reason)
        elif host != "localhost" and numeric_loopback(host) is None:
            raise SecurityBoundaryError(
                f"pinned-name contract allows only 'localhost' or a numeric loopback IP, "
                f"not {host!r}"
            )

    def validate(self, url: str) -> ValidatedTarget:
        scheme, host, port = _origin_parts(url)
        self._check_contract_host(host)
        if (scheme, host, port) != self.origin:
            raise SecurityBoundaryError(
                f"target origin differs from the local portal origin: {scheme}://{host}:{port}"
            )
        parts = urlsplit(url)
        path = parts.path or "/"
        target = path + (f"?{parts.query}" if parts.query else "")
        if not target.startswith("/"):
            raise SecurityBoundaryError("request target must be absolute")
        if "#" in target:
            raise SecurityBoundaryError("fragments must be stripped before validation")
        clean = urlunsplit((scheme, parts.netloc, path, parts.query, ""))
        return ValidatedTarget(clean, scheme, host, port, target)
