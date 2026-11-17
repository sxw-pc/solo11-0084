"""Minimal GET/HEAD HTTP client with a hard local-origin and peer boundary."""

from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass
from typing import Callable

from .errors import SecurityBoundaryError, TransportError
from .security import (
    AddressContract,
    LoopbackConnector,
    PeerMismatchError,
    ResolvedPeer,
    UrlPolicy,
    ValidatedTarget,
    literal_peer,
)

BlockedCallback = Callable[[str, str, dict], None]
_ALLOWED_EXTRA = frozenset({"range", "if-range"})


@dataclass
class HttpResponse:
    method: str
    url: str
    status: int
    headers: dict[str, str]
    _connection: http.client.HTTPConnection
    _response: http.client.HTTPResponse

    @property
    def content_length(self) -> int | None:
        value = self.headers.get("content-length")
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None

    def iter_chunks(self, size: int = 65536):
        if self.method == "HEAD":
            return
        while True:
            chunk = self._response.read(size)
            if not chunk:
                return
            yield chunk

    def read_limited(self, limit: int) -> bytes:
        length = self.content_length
        if length is not None and length > limit:
            raise TransportError(f"response exceeds {limit} bytes")
        data = self._response.read(limit + 1)
        if len(data) > limit:
            raise TransportError(f"response exceeds {limit} bytes")
        return data

    def text(self, limit: int = 1_000_000) -> str:
        return self.read_limited(limit).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self) -> "HttpResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that can only dial a pre-selected numeric loopback peer.

    ``http.client`` never sees the configured host name: the connection is
    constructed with the pinned numeric address and its socket factory is
    replaced, so ``connect()`` cannot trigger a second name resolution.
    """

    def __init__(self, peer: ResolvedPeer, *, timeout: float, connector: LoopbackConnector) -> None:
        if not peer.is_loopback:
            raise SecurityBoundaryError(f"refusing non-loopback peer {peer.text}")
        super().__init__(peer.address, peer.port, timeout=timeout)
        self._peer = peer
        self._connector = connector
        self._create_connection = self._connect_pinned

    def _connect_pinned(self, address, timeout=None, source_address=None):
        effective = self.timeout if timeout is None else timeout
        return self._connector.connect(self._peer, effective)


class SafeHttpClient:
    """A client that validates every target and pins a numeric loopback peer."""

    def __init__(
        self,
        policy: UrlPolicy,
        *,
        timeout: float = 5.0,
        on_blocked: BlockedCallback | None = None,
        on_event: BlockedCallback | None = None,
        connector: LoopbackConnector | None = None,
        resolver=None,
        socket_factory=None,
    ) -> None:
        self.policy = policy
        self.timeout = timeout
        self.on_blocked = on_blocked
        self.on_event = on_event
        self.connector = connector or LoopbackConnector(resolver, socket_factory)

    # -- security audit -------------------------------------------------
    def _security_event(self, code: str, subject: str, detail: dict) -> None:
        if self.on_blocked is not None:
            self.on_blocked(code, subject, detail)
        if self.on_event is not None and self.on_event is not self.on_blocked:
            self.on_event(code, subject, detail)

    def _blocked(self, url: str, detail: str) -> None:
        self._security_event("target.blocked", url, {"reason": detail})
        raise SecurityBoundaryError(detail)

    def validate(self, url: str) -> ValidatedTarget:
        try:
            return self.policy.validate(url)
        except SecurityBoundaryError as exc:
            self._blocked(url, str(exc))
            raise

    # -- address contract -----------------------------------------------
    def pin(self, target: ValidatedTarget) -> ResolvedPeer:
        """Select the single numeric loopback peer used for this target."""
        if self.policy.contract is AddressContract.LITERAL:
            return literal_peer(target.host, target.port)
        if target.host != "localhost":
            return literal_peer(target.host, target.port)
        try:
            candidates = self.connector.resolve(target.host, target.port)
        except (OSError, socket.gaierror) as exc:
            detail = {"reason": f"cannot resolve {target.host!r}: {exc}"}
            self._security_event("target.resolution_failed", target.url, detail)
            raise TransportError(detail["reason"]) from exc
        allowed = [
            peer
            for peer in candidates
            if peer.is_loopback and peer.port == target.port
        ]
        rejected = [
            peer
            for peer in candidates
            if not (peer.is_loopback and peer.port == target.port)
        ]
        if rejected:
            self._security_event(
                "target.resolution_filtered",
                target.url,
                {
                    "reason": "non-loopback or wrong-port resolver results were discarded",
                    "discarded": [peer.text for peer in rejected],
                },
            )
        if not allowed:
            detail = {
                "reason": "resolver returned no numeric loopback address",
                "candidates": [peer.text for peer in candidates],
            }
            self._security_event("target.resolution_rejected", target.url, detail)
            raise SecurityBoundaryError(detail["reason"])
        allowed.sort(key=lambda peer: 0 if peer.family == socket.AF_INET else 1)
        return allowed[0]

    # -- request --------------------------------------------------------
    def open(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        method = method.upper()
        if method not in {"GET", "HEAD"}:
            raise ValueError("only GET and HEAD are implemented")
        target = self.validate(url)
        request_headers = {
            "Host": target.host_header,
            "User-Agent": "releaseharbor/1.0 (offline-fixture-client)",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        for name, value in (headers or {}).items():
            key = name.lower()
            if key not in _ALLOWED_EXTRA:
                raise ValueError(f"header not permitted: {name}")
            if "\r" in value or "\n" in value:
                raise ValueError("header values must not contain newlines")
            request_headers[name] = value
        peer = self.pin(target)
        connection = _PinnedHTTPConnection(
            peer, timeout=self.timeout, connector=self.connector
        )
        try:
            connection.request(method, target.request_target, headers=request_headers)
            response = connection.getresponse()
        except PeerMismatchError as exc:
            connection.close()
            self._security_event(
                "target.peer_mismatch", target.url, {"reason": str(exc), "peer": peer.text}
            )
            raise
        except SecurityBoundaryError as exc:
            connection.close()
            self._security_event(
                "target.connect_blocked", target.url, {"reason": str(exc), "peer": peer.text}
            )
            raise
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            self._security_event(
                "target.connect_failed", target.url, {"reason": str(exc), "peer": peer.text}
            )
            raise TransportError(f"{method} {target.url} failed: {exc}") from exc
        merged = {name.lower(): value for name, value in response.getheaders()}
        return HttpResponse(method, target.url, response.status, merged, connection, response)
