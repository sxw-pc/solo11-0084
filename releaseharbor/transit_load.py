"""Ledger reads, on-disk re-verification, and parsing for a transit query."""

from __future__ import annotations

from datetime import date

from .errors import IntegrityError
from .transit_csv import parse_edges, parse_labels, parse_stations
from .transit_model import (
    NEW_ROLE,
    OLD_ROLE,
    ArtifactEvidence,
    Diagnostic,
    EdgeTable,
    SiteSide,
    TransitInputs,
)
from .verify import read_verified_artifact


def _row(connection, spec, version: str, filename: str):
    return connection.execute(
        "SELECT * FROM artifacts WHERE dataset_id = ? AND version_id = ? AND filename = ?",
        (spec.dataset_id, version, filename),
    ).fetchone()


def _read(connection, config, spec, role, version, filename):
    row = _row(connection, spec, version, filename)
    if row is None:
        raise IntegrityError("missing_artifact", f"{version}/{filename} is not in the ledger")
    if row["status"] not in {"verified", "skipped"} or not row["local_path"]:
        raise IntegrityError(
            "artifact_not_accepted",
            f"{version}/{filename} status is {row['status']}, not an accepted artifact",
        )
    data, report = read_verified_artifact(
        config.root,
        config.download.artifact_dir,
        row["local_path"],
        int(row["expected_size"]),
        row["expected_sha256"],
    )
    evidence = ArtifactEvidence(
        role=role,
        dataset_id=row["dataset_id"],
        version_id=row["version_id"],
        filename=row["filename"],
        local_path=row["local_path"],
        expected_size=int(row["expected_size"]),
        expected_sha256=row["expected_sha256"],
        status=row["status"],
    )
    return data, evidence, report


def _version_date(connection, spec, version: str) -> str | None:
    row = connection.execute(
        "SELECT released_at FROM versions WHERE dataset_id = ? AND version_id = ?",
        (spec.dataset_id, version),
    ).fetchone()
    return None if row is None else row["released_at"]


def _iso(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _attach_labels(side: SiteSide, labels, filename: str, role: str) -> list[Diagnostic]:
    problems = []
    for code in sorted(side.stations):
        if code not in labels:
            problems.append(
                Diagnostic(
                    "missing_zone",
                    role,
                    filename,
                    None,
                    f"station {code} has no zone label",
                )
            )
    for code in sorted(labels):
        if code not in side.stations:
            problems.append(
                Diagnostic(
                    "label_without_station",
                    role,
                    filename,
                    labels[code].line,
                    f"zone label for {code} has no station count",
                )
            )
    side.labels = dict(labels)
    return problems


def _load_side(connection, config, spec, role, version, counts_file, labels_file, inputs):
    evidence = {}
    label_version = (
        spec.old_label_release if role == OLD_ROLE else spec.new_label_release
    )
    try:
        station_data, count_evidence, count_report = _read(
            connection, config, spec, role, version, counts_file
        )
        label_data, label_evidence, label_report = _read(
            connection, config, spec, f"{role}_labels", label_version, labels_file
        )
    except IntegrityError as exc:
        inputs.diagnostics.append(
            Diagnostic(getattr(exc, "code", "integrity_error"), role, "", None, str(exc))
        )
        return None
    stations, station_problems = parse_stations(station_data, role, counts_file)
    labels, label_problems = parse_labels(label_data, f"{role}_labels", labels_file)
    side = SiteSide(
        role=role,
        version_id=version,
        file_sha256=count_report.actual_sha256,
        file_size=count_report.actual_size,
        local_path=count_evidence.local_path or "",
        label_sha256=label_report.actual_sha256,
        label_size=label_report.actual_size,
        stations=stations,
        labels=labels,
    )
    inputs.diagnostics.extend(station_problems)
    inputs.diagnostics.extend(label_problems)
    if not station_problems and not label_problems:
        inputs.diagnostics.extend(_attach_labels(side, labels, labels_file, f"{role}_labels"))
    evidence["counts"] = count_evidence.as_dict()
    evidence["labels"] = label_evidence.as_dict()
    return side, evidence


def _load_edges(connection, config, spec, inputs, old_date):
    refs = []
    seen: dict[tuple[str, str], str] = {}
    for table in spec.edge_tables:
        version = table["version"]
        filename = table["filename"]
        try:
            data, evidence, report = _read(connection, config, spec, "edges", version, filename)
        except IntegrityError as exc:
            inputs.diagnostics.append(
                Diagnostic(
                    getattr(exc, "code", "integrity_error"), "edges", filename, None, str(exc)
                )
            )
            continue
        edges, problems = parse_edges(data, "edges", version, filename)
        version_date = _version_date(connection, spec, version)
        refs.append(
            {
                **evidence.as_dict(),
                "version_date": version_date,
                "observed_size": report.actual_size,
                "observed_sha256": report.actual_sha256,
                "edge_count": len(edges),
            }
        )
        inputs.diagnostics.extend(problems)
        if problems:
            continue
        if old_date is not None and version_date is not None and version_date < old_date:
            inputs.edge_tables = []
            inputs.diagnostics.append(
                Diagnostic(
                    "version_inversion",
                    "edges",
                    filename,
                    None,
                    f"edge table {version} ({version_date}) predates the base release "
                    f"{spec.old_version} ({old_date})",
                )
            )
            return refs
        for edge in edges:
            key = (edge.source, edge.target)
            if key in seen:
                inputs.diagnostics.append(
                    Diagnostic(
                        "duplicate_edge",
                        "edges",
                        filename,
                        edge.line,
                        f"edge {edge.source}->{edge.target} is also declared by {seen[key]}",
                    )
                )
                continue
            seen[key] = version
        inputs.edge_tables.append(
            EdgeTable(
                version_id=version,
                file_sha256=report.actual_sha256,
                file_size=report.actual_size,
                local_path=evidence.local_path or "",
                edges=edges,
            )
        )
    return refs


def collect_inputs(connection, config, spec) -> TransitInputs:
    inputs = TransitInputs(dataset_id=spec.dataset_id)
    old_date = _version_date(connection, spec, spec.old_version)
    new_date = _version_date(connection, spec, spec.new_version)
    if _iso(old_date) and _iso(new_date) and _iso(new_date) < _iso(old_date):
        inputs.diagnostics.append(
            Diagnostic(
                "version_inversion",
                "model",
                "",
                None,
                f"next version {spec.new_version} ({new_date}) predates the base version "
                f"{spec.old_version} ({old_date})",
            )
        )
        return inputs
    old_loaded = _load_side(
        connection, config, spec, OLD_ROLE, spec.old_version,
        spec.old_counts, spec.old_labels, inputs,
    )
    new_loaded = _load_side(
        connection, config, spec, NEW_ROLE, spec.new_version,
        spec.new_counts, spec.new_labels, inputs,
    )
    if old_loaded:
        inputs.old = old_loaded[0]
        inputs.old_evidence = old_loaded[1]["counts"]
        inputs.old_label_evidence = old_loaded[1]["labels"]
    if new_loaded:
        inputs.new = new_loaded[0]
        inputs.new_evidence = new_loaded[1]["counts"]
        inputs.new_label_evidence = new_loaded[1]["labels"]
    inputs.edge_evidence = _load_edges(connection, config, spec, inputs, old_date)
    return inputs
