"""Shared fixtures for ReleaseHarbor unit tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from releaseharbor.discovery import ArtifactRef
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.portal_server import PortalServer

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "portal.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_project(
    root: Path,
    port: int,
    *,
    retries: int = 2,
    channels=None,
    fixture_obj: dict | None = None,
    discovery_override: dict | None = None,
) -> Path:
    fixture = root / "fixtures" / "portal.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if fixture_obj is None:
        shutil.copyfile(FIXTURE, fixture)
    else:
        fixture.write_text(json.dumps(fixture_obj, indent=2) + "\n", encoding="utf-8")
    discovery = {
        "max_datasets": 20,
        "max_versions": 10,
        "max_artifacts": 20,
        "max_pages": 100,
        "channels": channels or ["stable", "beta"],
    }
    discovery.update(discovery_override or {})
    config = {
        "schema_version": 1,
        "root": "..",
        "portal": {
            "fixture": "fixtures/portal.json",
            "bind_host": "127.0.0.1",
            "base_url": f"http://127.0.0.1:{port}",
        },
        "discovery": discovery,
        "download": {
            "work_dir": "var",
            "artifact_dir": "var/artifacts",
            "quarantine_dir": "var/quarantine",
            "db_path": "var/releaseharbor.sqlite3",
            "report_dir": "reports",
            "retries": retries,
            "retry_backoff_ms": 0,
            "chunk_size": 4096,
            "max_artifact_bytes": 2097152,
        },
        "security": {"request_timeout_seconds": 3},
        "clock": {"mode": "fixed", "fixed": "2026-01-15T12:00:00Z"},
        "console": {"port": 18766},
        "demo": {"reset_on_start": True},
    }
    path = root / "config" / "demo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def portal_for(config) -> PortalServer:
    fixture = load_portal_fixture(config.portal.fixture)
    return PortalServer(fixture, "127.0.0.1", int(config.portal.base_url.rsplit(":", 1)[1]))


def ref_for(config, dataset: str, version: str, filename: str) -> ArtifactRef:
    fixture = load_portal_fixture(config.portal.fixture)
    ds = next(item for item in fixture.datasets if item.dataset_id == dataset)
    ver = next(item for item in ds.versions if item.version_id == version)
    art = next(item for item in ver.artifacts if item.name == filename)
    return ArtifactRef(
        dataset_id=ds.dataset_id,
        dataset_title=ds.title,
        version_id=ver.version_id,
        version_label=ver.label,
        channel=ver.channel,
        released_at=ver.released_at,
        filename=art.name,
        url=f"{config.portal.base_url}/files/{ds.dataset_id}/{ver.version_id}/{art.name}",
        media_type=art.media_type,
        expected_size=art.declared_size,
        expected_sha256=art.declared_sha256,
    )


def _art(name: str, content: str) -> dict:
    return {
        "name": name,
        "media_type": "text/plain",
        "content": content,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _ver(version_id: str, artifacts: list, channel: str = "stable") -> dict:
    return {
        "id": version_id,
        "label": f"Version {version_id}",
        "channel": channel,
        "released_at": "2026-01-01",
        "artifacts": artifacts,
    }


def small_fixture(*, alpha_extra: bool = False, alpha_v2_channel: str = "stable") -> dict:
    """Portal home lists alpha/beta; alpha lists v1/v2; v1 lists two files."""
    alpha_versions = [
        _ver("v1", [_art("a-one.txt", "alpha one\n"), _art("a-two.txt", "alpha two\n")]),
        _ver("v2", [_art("a-v2.txt", "alpha v2\n")], channel=alpha_v2_channel),
    ]
    if alpha_extra:
        alpha_versions.append(_ver("v3", [_art("a-v3.txt", "alpha v3\n")]))
    datasets = []
    for name in ("alpha", "beta"):
        versions = alpha_versions if name == "alpha" else [
            _ver(v, [_art(f"b-{v}.txt", f"beta {v}\n")]) for v in ("v1", "v2")
        ]
        datasets.append(
            {
                "id": name,
                "title": f"{name.title()} Dataset",
                "summary": f"{name} summary",
                "publisher": f"{name.title()} Office",
                "license": "CC0-1.0 (synthetic fixture)",
                "versions": versions,
            }
        )
    return {
        "schema_version": 1,
        "portal_title": "Coverage Sandbox",
        "portal_description": "synthetic coverage portal",
        "publisher": "Coverage Fixture Guild",
        "datasets": datasets,
    }


def home_page(*dataset_ids: str) -> str:
    cards = "".join(
        f'<a class="dataset-link" href="/datasets/{item}" data-dataset-id="{item}">{item}</a>'
        for item in dataset_ids
    )
    return f'<!doctype html><html><body><main data-page="home">{cards}</main></body></html>'


def dataset_page(dataset_id: str, *version_ids: str) -> str:
    rows = "".join(
        f'<a class="version-link" href="/datasets/{dataset_id}/versions/{v}" '
        f'data-version-id="{v}">{v}</a>'
        for v in version_ids
    )
    return (
        f'<!doctype html><html><body><main data-page="dataset" '
        f'data-dataset-id="{dataset_id}" data-title="T {dataset_id}" '
        f'data-publisher="P" data-license="L">{rows}</main></body></html>'
    )


def version_page(dataset_id: str, version_id: str, anchors: list, channel: str = "stable") -> str:
    rows = "".join(
        f'<a class="artifact-link" href="{a["href"]}" data-filename="{a["name"]}" '
        f'data-size="{a["size"]}" data-sha256="{a["sha256"]}" '
        f'data-media-type="text/plain">{a["name"]}</a>'
        for a in anchors
    )
    return (
        f'<!doctype html><html><body><main data-page="version" '
        f'data-dataset-id="{dataset_id}" data-version-id="{version_id}" '
        f'data-version-label="L {version_id}" data-channel="{channel}" '
        f'data-released-at="2026-01-01">{rows}</main></body></html>'
    )


def good_anchor(dataset_id: str, version_id: str, name: str, content: str = "x\n") -> dict:
    return {
        "href": f"/files/{dataset_id}/{version_id}/{name}",
        "name": name,
        "size": len(content.encode("utf-8")),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


class PageServer:
    """Minimal loopback server handing out crafted pages (a synthetic portal)."""

    def __init__(self, pages: dict) -> None:
        self.pages = pages

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                return

            def do_GET(self) -> None:
                item = self.server.pages.get(urlsplit(self.path).path)  # type: ignore[attr-defined]
                if item is None:
                    status, text = 404, "not found"
                else:
                    status, text = item
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._server.pages = pages  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "PageServer":
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="coverage-page-server", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()
