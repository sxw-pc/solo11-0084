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


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_project(root: Path, port: int, *, retries: int = 2, channels=None) -> Path:
    fixture = root / "fixtures" / "portal.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, fixture)
    config = {
        "schema_version": 1,
        "root": "..",
        "portal": {
            "fixture": "fixtures/portal.json",
            "bind_host": "127.0.0.1",
            "base_url": f"http://127.0.0.1:{port}",
        },
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
    return PortalServer(fixture, "127.0.0.1", int(config.portal.base_url.rsplit(":", 1)[1]))


def registry_spec(orgs, aliases, groups=(), exclusions=(), version="reg-1") -> dict:
    """Build a synthetic publisher-attribution registry document."""
    return {
        "schema_version": 1,
        "registry_version": version,
        "groups": [{"id": gid, "name": f"Group {gid}"} for gid in groups],
        "organizations": [
            {"id": oid, "legal_name": f"Legal {oid}", **({"group": grp} if grp else {})}
            for oid, grp in orgs
        ],
        "aliases": [
            {"display_name": name, "valid_from": start, "valid_to": end, "candidates": cands}
            for name, start, end, cands in aliases
        ],
        "exclusions": [
            {
                "note": "synthetic mutual exclusion",
                "literals": [{"display_name": name, "org_id": oid} for name, oid in pairs],
            }
            for pairs in exclusions
        ],
    }


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
