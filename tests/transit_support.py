"""Shared controlled fixtures for the transit bound tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.config import load_config
from releaseharbor.db import connect_readonly
from releaseharbor.pipeline import run_pipeline
from releaseharbor.transit_report import write_transit_result
from releaseharbor.transit import QuerySpec, load_query_spec, query_bounds

BASE_COUNTS = "station_code,station_name,weekday_boardings\nA,Alpha,7\nB,Bravo,3\n"
BASE_LABELS = "station_code,short_label,zone\nA,Alpha,E\nB,Bravo,W\n"
NEXT_COUNTS = "station_code,station_name,weekday_boardings\nC,Charlie,5\nD,Delta,5\n"
NEXT_LABELS = "station_code,short_label,zone\nC,Charlie,E\nD,Delta,W\n"

FOUR_EDGES = [("A", "C", 0, 99), ("A", "D", 0, 99), ("B", "C", 0, 99), ("B", "D", 0, 99)]


def _csv(rows: str) -> bytes:
    return rows.encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def edges_csv(rows) -> bytes:
    return _csv("from,to,min,max\n" + "".join(f"{a},{b},{lo},{hi}\n" for a, b, lo, hi in rows))


def files_for(**overrides) -> dict:
    files = {
        "stations.csv": _csv(BASE_COUNTS),
        "station_labels.csv": _csv(BASE_LABELS),
        "next_stations.csv": _csv(NEXT_COUNTS),
        "next_labels.csv": _csv(NEXT_LABELS),
        "flow_edges.csv": edges_csv(FOUR_EDGES),
    }
    files.update(overrides)
    return files


def versions_for(extra=None) -> dict:
    versions = {
        "v1": {"released_at": "2025-01-10", "files": ["stations.csv", "station_labels.csv"]},
        "v2": {"released_at": "2025-02-10", "files": ["next_stations.csv", "next_labels.csv"]},
        "edges-v1": {"released_at": "2025-02-11", "files": ["flow_edges.csv"]},
    }
    versions.update(extra or {})
    return versions


def spec(**overrides) -> QuerySpec:
    values = dict(
        dataset_id="transit-counts",
        old_version="v1",
        new_version="v2",
        old_zone="E",
        new_zone="W",
        new_counts="next_stations.csv",
        new_labels="next_labels.csv",
        edge_tables=[{"version": "edges-v1", "filename": "flow_edges.csv"}],
    )
    values.update(overrides)
    return QuerySpec(**values)


def contract(result: dict, name: str) -> dict:
    return next(item for item in result["contracts"] if item["contract"] == name)


class TransitFixture:
    """Builds a throw-away project whose CSV bytes and digests are controlled."""

    def __init__(self, root: Path, files: dict, versions: dict, *, declared_sha=None,
                 dataset_id: str = "transit-counts", resolved_versions=None):
        self.root = root
        self.port = free_port()
        self.files = files
        dataset = {
            "id": dataset_id,
            "title": "Commuter Counts",
            "summary": "Controlled station counts.",
            "publisher": "Fixture Transit Unit",
            "license": "CC0-1.0 (synthetic)",
            "versions": [],
        }
        for version, meta in versions.items():
            source_versions = resolved_versions or {}
            version_id = source_versions.get(version, version)
            artifacts = []
            for filename in meta["files"]:
                content = files[filename]
                artifacts.append(
                    {
                        "name": filename,
                        "media_type": "text/csv",
                        "content": content.decode("utf-8"),
                        "sha256": (declared_sha or {}).get(filename, _sha(content)),
                    }
                )
            dataset["versions"].append(
                {
                    "id": version_id,
                    "label": version_id,
                    "channel": "stable",
                    "released_at": meta["released_at"],
                    "artifacts": artifacts,
                }
            )
        fixture = {
            "schema_version": 1,
            "portal_title": "Transit sandbox",
            "portal_description": "Synthetic flow test portal.",
            "publisher": "Transit Fixture Guild",
            "datasets": [dataset],
        }
        self.config_path = write_project(root, self.port, channels=["stable"])
        _write_json(root / "fixtures/portal.json", fixture)
        self.config = load_config(self.config_path)
        self.portal = portal_for(self.config)

    def start(self):
        self.portal.start()
        return self

    def stop(self):
        self.portal.stop()

    def run(self):
        return run_pipeline(self.config, reset=True)

    def query(self, query_spec: QuerySpec) -> dict:
        connection = connect_readonly(self.config.download.db_path)
        try:
            return query_bounds(self.config, connection, query_spec)
        finally:
            connection.close()


class TransitBaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build(self, *, files=None, versions=None, declared_sha=None):
        fixture = TransitFixture(
            self.root, files or files_for(), versions or versions_for(), declared_sha=declared_sha
        )
        fixture.start()
        self.addCleanup(fixture.stop)
        fixture.run()
        self.fixture = fixture
        return fixture
