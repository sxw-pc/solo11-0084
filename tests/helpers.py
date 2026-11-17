"""Shared fixtures for ReleaseHarbor unit tests."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

from releaseharbor.discovery import ArtifactRef
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.portal_server import PortalServer

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "portal.json"
EXTERNAL_V4 = "192.0.2.55"
EXTERNAL_V6 = "2001:db8::55"


def addr_infos(port: int, *addresses: str) -> list[tuple]:
    """Build getaddrinfo-shaped results for the given numeric addresses."""
    results = []
    for address in addresses:
        if ":" in address:
            results.append(
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))
            )
        else:
            results.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)))
    return results


def event_sink() -> tuple[list[dict], "callable"]:
    events: list[dict] = []

    def sink(code: str, subject: str, detail: dict) -> None:
        events.append({"code": code, "subject": subject, "detail": detail})

    return events, sink


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
    host: str = "127.0.0.1",
    contract: str | None = None,
) -> Path:
    fixture = root / "fixtures" / "portal.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, fixture)
    portal = {
        "fixture": "fixtures/portal.json",
        "bind_host": host,
        "base_url": f"http://{host}:{port}",
    }
    if contract is not None:
        portal["address_contract"] = contract
    config = {
        "schema_version": 1,
        "root": "..",
        "portal": portal,
        "discovery": {
            "max_datasets": 20,
            "max_versions": 10,
            "max_artifacts": 20,
            "max_pages": 100,
            "channels": channels or ["stable", "beta"],
        },
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
    port = int(config.portal.base_url.rsplit(":", 1)[1])
    return PortalServer(fixture, config.portal.bind_host, port)


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
