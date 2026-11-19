"""Shared fixtures for ReleaseHarbor unit tests."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

from releaseharbor.discovery import ArtifactRef
from releaseharbor.downloader import artifact_destination
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.portal_server import PortalServer
from releaseharbor.util import sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "portal.json"

CHUNK_SIZE = 128
CHUNKED_CONTENT = b"A" * 128 + b"B" * 128 + b"C" * 128 + b"D" * 64
SWAPPED_CONTENT = b"a" * 128 + b"b" * 128 + b"c" * 128 + b"d" * 64


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
    download_overrides=None,
    fixture_text=None,
) -> Path:
    fixture = root / "fixtures" / "portal.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if fixture_text is None:
        shutil.copyfile(FIXTURE, fixture)
    else:
        fixture.write_text(fixture_text, encoding="utf-8")
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
    config["download"].update(download_overrides or {})
    path = root / "config" / "demo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _chunks(data: bytes) -> list[bytes]:
    return [data[index : index + CHUNK_SIZE] for index in range(0, len(data), CHUNK_SIZE)]


def chunked_fixture_text() -> str:
    """Synthetic portal with a 4-chunk artifact (short last chunk) and fault peers."""
    base = {
        "media_type": "application/octet-stream",
        "content": CHUNKED_CONTENT.decode("ascii"),
        "sha256": sha256_bytes(CHUNKED_CONTENT),
        "chunk_size": CHUNK_SIZE,
    }
    artifacts = [
        {"name": "payload.bin", **base},
        {"name": "extra-byte.bin", **base, "range_extra_bytes": 1},
        {"name": "bad-total.bin", **base, "range_total_delta": 1},
        {"name": "swapped.bin", **base, "range_content": SWAPPED_CONTENT.decode("ascii")},
        {"name": "wrong-digest.bin", **base, "sha256": "0" * 64},
        {
            "name": "stale-chunks.bin",
            **base,
            "chunk_sha256": [sha256_bytes(part) for part in _chunks(SWAPPED_CONTENT)],
        },
        {
            "name": "broken-manifest.bin",
            **base,
            "declared_size": len(CHUNKED_CONTENT) + CHUNK_SIZE,
        },
        {
            "name": "empty.bin",
            "media_type": "application/octet-stream",
            "content": "",
            "sha256": sha256_bytes(b""),
            "chunk_size": CHUNK_SIZE,
        },
    ]
    fixture = {
        "schema_version": 1,
        "portal_title": "Chunked Fixture Portal",
        "portal_description": "Synthetic chunk-manifest acceptance fixtures.",
        "publisher": "Aurora Fixture Data Guild",
        "datasets": [
            {
                "id": "chunked-releases",
                "title": "Chunked Release Fixtures",
                "summary": "Four-chunk release artifacts with repair fault injection.",
                "publisher": "Aurora Fixture Data Guild",
                "license": "CC0-1.0 (synthetic fixture)",
                "versions": [
                    {
                        "id": "v1",
                        "label": "Chunked v1",
                        "channel": "stable",
                        "released_at": "2026-01-02",
                        "notes": "Synthetic chunk acceptance fixtures.",
                        "artifacts": artifacts,
                    }
                ],
            }
        ],
    }
    return json.dumps(fixture, indent=1)


def write_chunked_project(
    root: Path,
    port: int,
    *,
    contract: str = "repair",
    max_recovery_bytes: int = 4096,
    chunk_retries: int = 2,
    retries: int = 2,
) -> Path:
    return write_project(
        root,
        port,
        retries=retries,
        fixture_text=chunked_fixture_text(),
        download_overrides={
            "recovery_contract": contract,
            "max_recovery_bytes": max_recovery_bytes,
            "chunk_retries": chunk_retries,
        },
    )


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
        chunk_size=art.chunk_size,
        chunk_sha256=art.declared_chunk_sha256,
    )


def seed_partial(config, ref: ArtifactRef, content: bytes) -> Path:
    target = artifact_destination(
        config.download.artifact_dir, ref.dataset_id, ref.version_id, ref.filename
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(content)
    return partial


class CountingClient:
    """SafeHttpClient wrapper that records every request for assertions."""

    def __init__(self, inner):
        self.inner = inner
        self.requests = []

    def validate(self, url):
        return self.inner.validate(url)

    def open(self, method, url, headers=None):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers or {})}
        )
        return self.inner.open(method, url, headers)

    def gets(self) -> list[dict]:
        return [request for request in self.requests if request["method"] == "GET"]
