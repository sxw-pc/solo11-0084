"""Offline cross-version transit bounds built only from accepted release CSV files.

The query reads the ledger, re-verifies every CSV on disk, parses stations,
zone labels, and allowed-flow edges, then solves the integer flow problem.  It
never mutates artifacts, runs, or events: a new edge-table version changes only
the bounds result, and older results stay readable with the digests they used.

A single unusable input (rejected artifact, tampered file, duplicate station,
missing zone, unknown node, negative capacity, inverted versions, infeasible
flow) suppresses every numeric bound and returns a located reason instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .transit_contracts import (
    infeasible_contracts,
    numeric_contracts,
    unknown_contracts,
)
from .transit_flow import Bounds, FlowModelError, Infeasible, build_network, compute_bounds
from .transit_load import collect_inputs
from .transit_model import Diagnostic, TransitInputs


@dataclass
class QuerySpec:
    dataset_id: str
    old_version: str
    new_version: str
    old_zone: str
    new_zone: str
    old_counts: str = "stations.csv"
    new_counts: str = "stations.csv"
    old_labels: str = "station_labels.csv"
    new_labels: str = "station_labels.csv"
    old_label_version: str | None = None
    new_label_version: str | None = None
    edge_tables: list[dict] = field(default_factory=list)

    @property
    def old_label_release(self) -> str:
        return self.old_label_version or self.old_version

    @property
    def new_label_release(self) -> str:
        return self.new_label_version or self.new_version


def load_query_spec(path: Path) -> QuerySpec:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("transit query must be a JSON object")
    required = {"dataset_id", "old_version", "new_version", "old_zone", "new_zone"}
    missing = required - set(raw)
    if missing:
        raise ValueError("transit query is missing: " + ", ".join(sorted(missing)))
    for key in required:
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError(f"transit query field {key} must be a non-empty string")
    tables = raw.get("edge_tables", [])
    if not isinstance(tables, list) or not tables:
        raise ValueError("transit query needs a non-empty edge_tables list")
    clean_tables = []
    for item in tables:
        if not isinstance(item, dict) or not isinstance(item.get("version"), str):
            raise ValueError("each edge table needs a version id")
        clean_tables.append(
            {
                "version": item["version"],
                "filename": item.get("filename", "flow_edges.csv"),
            }
        )
    return QuerySpec(
        dataset_id=raw["dataset_id"],
        old_version=raw["old_version"],
        new_version=raw["new_version"],
        old_zone=raw["old_zone"],
        new_zone=raw["new_zone"],
        old_counts=raw.get("old_counts", "stations.csv"),
        new_counts=raw.get("new_counts", "stations.csv"),
        old_labels=raw.get("old_labels", "station_labels.csv"),
        new_labels=raw.get("new_labels", "station_labels.csv"),
        old_label_version=raw.get("old_label_version"),
        new_label_version=raw.get("new_label_version"),
        edge_tables=clean_tables,
    )


def _reason(exc: Exception, role: str = "model", filename: str = "") -> dict:
    return {
        "code": getattr(exc, "code", "error"),
        "role": role,
        "filename": filename,
        "line": None,
        "detail": getattr(exc, "detail", str(exc)),
    }


def _with_zones(spec: QuerySpec, old_zone, new_zone) -> QuerySpec:
    if old_zone is None and new_zone is None:
        return spec
    return QuerySpec(
        dataset_id=spec.dataset_id,
        old_version=spec.old_version,
        new_version=spec.new_version,
        old_zone=spec.old_zone if old_zone is None else old_zone,
        new_zone=spec.new_zone if new_zone is None else new_zone,
        old_counts=spec.old_counts,
        new_counts=spec.new_counts,
        old_labels=spec.old_labels,
        new_labels=spec.new_labels,
        old_label_version=spec.old_label_version,
        new_label_version=spec.new_label_version,
        edge_tables=spec.edge_tables,
    )


def _infeasible(spec: QuerySpec, inputs: TransitInputs, exc: Infeasible, note) -> dict:
    conflict = dict(exc.conflict)
    conflict["old_zone"] = spec.old_zone
    conflict["new_zone"] = spec.new_zone
    return infeasible_contracts(spec, inputs, conflict, note)


def query_bounds(config, connection, spec: QuerySpec, old_zone=None, new_zone=None) -> dict:
    spec = _with_zones(spec, old_zone, new_zone)
    inputs = collect_inputs(connection, config, spec)
    if inputs.diagnostics:
        return unknown_contracts(spec, inputs, [d.as_dict() for d in inputs.diagnostics])
    try:
        network = build_network(inputs, spec.old_zone, spec.new_zone, inputs.edge_evidence)
    except FlowModelError as exc:
        inputs.diagnostics.append(Diagnostic(exc.code, "model", "", None, exc.detail))
        return unknown_contracts(spec, inputs, [_reason(exc)])
    try:
        bounds: Bounds = compute_bounds(network)
    except Infeasible as exc:
        return _infeasible(spec, inputs, exc, network.note)
    except FlowModelError as exc:
        inputs.diagnostics.append(Diagnostic(exc.code, "solve", "", None, exc.detail))
        return unknown_contracts(spec, inputs, [_reason(exc)])
    return numeric_contracts(spec, inputs, bounds, network.note)
