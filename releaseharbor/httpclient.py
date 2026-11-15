"""Minimal GET/HEAD HTTP client with a hard local-origin boundary."""

from __future__ import annotations

import http.client
from dataclasses import dataclass
from typing import Callable

from .errors import SecurityBoundaryError, TransportError
from .security import UrlPolicy, ValidatedTarget

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


class SafeHttpClient:
    """A client that validates every target before opening a socket."""

    def __init__(
        self,
        policy: UrlPolicy,
        *,
        timeout: float = 5.0,
        on_blocked: BlockedCallback | None = None,
    ) -> None:
        self.policy = policy
        self.timeout = timeout
        self.on_blocked = on_blocked

    def _blocked(self, url: str, detail: str) -> None:
        if self.on_blocked is not None:
            self.on_blocked("target.blocked", url, {"reason": detail})
        raise SecurityBoundaryError(detail)

    def validate(self, url: str) -> ValidatedTarget:
        try:
            return self.policy.validate(url)
        except SecurityBoundaryError as exc:
            self._blocked(url, str(exc))
            raise

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
        connection = http.client.HTTPConnection(
            target.host, target.port, timeout=self.timeout
        )
        try:
            connection.request(method, target.request_target, headers=request_headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise TransportError(f"{method} {target.url} failed: {exc}") from exc
        merged = {name.lower(): value for name, value in response.getheaders()}
        return HttpResponse(method, target.url, response.status, merged, connection, response)
