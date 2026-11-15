"""Loopback-only URL and bind-address enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .errors import SecurityBoundaryError

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_host(host: str) -> bool:
    return (host or "").strip().lower().rstrip(".") in LOOPBACK_NAMES


def require_loopback_bind(host: str) -> str:
    normalized = (host or "").strip().lower()
    if not is_loopback_host(normalized):
        raise SecurityBoundaryError(f"refusing non-loopback bind address: {host!r}")
    return normalized


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


class UrlPolicy:
    """Only permits the exact configured local portal origin."""

    def __init__(self, base_url: str) -> None:
        self.origin = origin_of(base_url)
        self.base_url = base_url.rstrip("/")

    def validate(self, url: str) -> ValidatedTarget:
        scheme, host, port = _origin_parts(url)
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
