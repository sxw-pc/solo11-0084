"""Loopback HTTP server for the synthetic open-data portal."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlsplit

from .errors import SecurityBoundaryError
from .fixture import DatasetFixture, PortalFixture, VersionFixture
from .portal_pages import render_dataset, render_home, render_version
from .security import is_loopback_host, require_loopback_bind


class _PortalHTTPServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, address, handler, fixture: PortalFixture) -> None:
        self.fixture = fixture
        self.transient_budget = {
            (dataset.dataset_id, version.version_id, artifact.name): artifact.transient_failures
            for dataset in fixture.datasets
            for version in dataset.versions
            for artifact in version.artifacts
        }
        super().__init__(address, handler)


class _PortalV6HTTPServer(_PortalHTTPServer):
    address_family = socket.AF_INET6


def _find_dataset(fixture: PortalFixture, dataset_id: str) -> DatasetFixture | None:
    return next((item for item in fixture.datasets if item.dataset_id == dataset_id), None)


def _find_version(dataset: DatasetFixture, version_id: str) -> VersionFixture | None:
    return next((item for item in dataset.versions if item.version_id == version_id), None)


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    if not value.startswith("bytes=") or "," in value:
        return None
    spec = value[6:].strip()
    if "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if start_text == "":
            length = int(end_text)
            if length <= 0 or size <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ReleaseHarborFixture/1.0"
    sys_version = ""

    def log_message(self, format, *args) -> None:
        return

    @property
    def fixture(self) -> PortalFixture:
        return self.server.fixture  # type: ignore[attr-defined]

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
        *,
        empty_for_head: bool = True,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if (not empty_for_head or self.command != "HEAD") and body:
            self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>Error</title></head>"
            f"<body><h1>{status}</h1><p>{message}</p></body></html>\n"
        ).encode("utf-8")
        self._send(status, body, "text/html; charset=utf-8")

    def _segments(self) -> list[str] | None:
        path = urlsplit(self.path).path
        segments = [unquote(part) for part in path.split("/") if part]
        for segment in segments:
            if segment in {".", ".."} or "\\" in segment or "\x00" in segment:
                return None
        return segments

    def do_HEAD(self) -> None:
        self._dispatch(head=True)

    def do_GET(self) -> None:
        self._dispatch(head=False)

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send(
            405,
            b"method not allowed\n",
            "text/plain; charset=utf-8",
            {"Allow": "GET, HEAD"},
        )

    def _dispatch(self, *, head: bool) -> None:
        host = self.headers.get("Host", "")
        host_name = host.split(":", 1)[0].strip("[]").lower()
        if host_name and not is_loopback_host(host_name):
            self._error(421, "Only loopback Host headers are accepted.")
            return
        segments = self._segments()
        if segments is None:
            self._error(400, "Invalid path.")
            return
        if not segments:
            self._send(200, render_home(self.fixture).encode("utf-8"), "text/html; charset=utf-8")
            return
        if len(segments) == 2 and segments[0] == "datasets":
            dataset = _find_dataset(self.fixture, segments[1])
            if dataset is None:
                self._error(404, "Dataset not found.")
                return
            body = render_dataset(dataset, self.fixture).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if (
            len(segments) == 4
            and segments[0] == "datasets"
            and segments[2] == "versions"
        ):
            dataset = _find_dataset(self.fixture, segments[1])
            version = _find_version(dataset, segments[3]) if dataset else None
            if dataset is None or version is None:
                self._error(404, "Version not found.")
                return
            body = render_version(dataset, version, self.fixture).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if len(segments) == 4 and segments[0] == "files":
            self._serve_artifact(segments[1], segments[2], segments[3], head=head)
            return
        self._error(404, "Route not found.")

    def _serve_artifact(self, dataset_id: str, version_id: str, filename: str, *, head: bool) -> None:
        dataset = _find_dataset(self.fixture, dataset_id)
        version = _find_version(dataset, version_id) if dataset else None
        if dataset is None or version is None:
            self._error(404, "Artifact not found.")
            return
        artifact = next((item for item in version.artifacts if item.name == filename), None)
        if artifact is None:
            self._error(404, "Artifact not found.")
            return
        key = (dataset_id, version_id, filename)
        budget = self.server.transient_budget  # type: ignore[attr-defined]
        if not head and budget.get(key, 0) > 0:
            budget[key] = budget[key] - 1
            self._send(
                503,
                b"temporary fixture fault\n",
                "text/plain; charset=utf-8",
                {"Retry-After": "0"},
            )
            return
        size = artifact.actual_size
        common = {
            "Accept-Ranges": "bytes",
            "ETag": f'"{artifact.actual_sha256}"',
            "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT",
            "Content-Disposition": f'attachment; filename="{artifact.name}"',
        }
        if head:
            self.send_response(200)
            self.send_header("Content-Type", artifact.media_type)
            self.send_header("Content-Length", str(size))
            for name, value in common.items():
                self.send_header(name, value)
            self.end_headers()
            return
        range_header = self.headers.get("Range")
        if range_header:
            parsed = _parse_range(range_header, size)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            body = artifact.content[start : end + 1]
            self._send(
                206,
                body,
                artifact.media_type,
                {**common, "Content-Range": f"bytes {start}-{end}/{size}"},
            )
            return
        self._send(200, artifact.content, artifact.media_type, common)


class PortalServer:
    """Owns a local mock portal HTTP server and its background thread."""

    def __init__(
        self, fixture: PortalFixture, bind_host: str = "127.0.0.1", port: int = 0
    ) -> None:
        host = require_loopback_bind(bind_host)
        if not 0 <= port <= 65535:
            raise SecurityBoundaryError("portal port is invalid")
        server_class = _PortalV6HTTPServer if ":" in host else _PortalHTTPServer
        try:
            self._server = server_class((host, port), _Handler, fixture)
        except OSError as exc:
            raise SecurityBoundaryError(f"cannot bind fixture portal: {exc}") from exc
        self._thread: threading.Thread | None = None
        self.host = host
        self.port = self._server.server_address[1]

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> "PortalServer":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="releaseharbor-fixture-portal", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> "PortalServer":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()
