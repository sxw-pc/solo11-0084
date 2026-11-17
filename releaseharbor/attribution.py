"""Offline publisher attribution and source-concentration analysis.

Each accepted release artifact is attributed to a fictional legal
organization through the local versioned registry: the run's preserved
publisher snapshot, the version's release date, and the accepted byte count
on disk. The analyzer enumerates every globally feasible assignment, so HHI
min/max bounds always come with complete assignment witnesses; unknown
evidence yields null bounds instead of a pseudo-precise total. The analysis
never rewrites byte acceptance, verified status, or license conclusions.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from math import prod
from pathlib import Path

from .errors import IntegrityError, SecurityBoundaryError
from .registry import Registry, is_day
from .util import resolve_under
from .verify import verify_file

MAX_ASSIGNMENTS = 4096
CONTRACTS = ("legal_entity", "group_control")


@dataclass
class _Variable:
    """One alias entry whose candidate choice binds every artifact using it."""

    display_name: str
    valid_from: str
    candidates: tuple[str, ...]
    weight: int = 0


def _disk_matches(root: Path, row: sqlite3.Row) -> bool:
    local_path = row["local_path"]
    size = row["actual_size"]
    digest = row["actual_sha256"]
    if not local_path or not isinstance(size, int) or not isinstance(digest, str):
        return False
    try:
        target = resolve_under(root, local_path, "local_path")
    except SecurityBoundaryError:
        return False
    try:
        report = verify_file(target, size, digest)
    except (IntegrityError, OSError):
        return False
    return report.accepted


def _feasible(variables: list[_Variable], combo: tuple[str, ...], exclusions) -> bool:
    active = {(variable.display_name, org) for variable, org in zip(variables, combo)}
    return not any(
        all(literal in active for literal in exclusion.literals) for exclusion in exclusions
    )


def _bucket(registry: Registry, contract: str, org_id: str) -> str:
    if contract == "group_control":
        group_id = registry.organizations[org_id].group_id
        if group_id:
            return group_id
    return org_id


def _witness(variables, combo, buckets, hhi, total) -> dict:
    return {
        "assignment": [
            {
                "display_name": variable.display_name,
                "org_id": org,
                "weight_bytes": variable.weight,
            }
            for variable, org in zip(variables, combo)
        ],
        "buckets": [
            {"bucket": key, "bytes": weight, "share": float(Fraction(weight, total))}
            for key, weight in sorted(buckets.items())
        ],
        "hhi": float(hhi),
    }


def _evaluate(registry, contract, variables, feasible, total) -> dict:
    evaluated = []
    for combo in feasible:
        buckets: dict[str, int] = {}
        for variable, org in zip(variables, combo):
            key = _bucket(registry, contract, org)
            buckets[key] = buckets.get(key, 0) + variable.weight
        hhi = sum(Fraction(weight * weight, total * total) for weight in buckets.values())
        evaluated.append((combo, buckets, hhi))
    low = min(item[2] for item in evaluated)
    high = max(item[2] for item in evaluated)
    lowest = min(item for item in evaluated if item[2] == low)
    highest = min(item for item in evaluated if item[2] == high)
    return {
        "status": "ok",
        "feasible_assignments": len(feasible),
        "hhi_min": float(low),
        "hhi_max": float(high),
        "min_witness": _witness(variables, lowest[0], lowest[1], low, total),
        "max_witness": _witness(variables, highest[0], highest[1], high, total),
        "min_witness_count": sum(1 for item in evaluated if item[2] == low),
        "max_witness_count": sum(1 for item in evaluated if item[2] == high),
    }


def _blank_contract(status: str) -> dict:
    return {
        "status": status,
        "feasible_assignments": None,
        "hhi_min": None,
        "hhi_max": None,
        "min_witness": None,
        "max_witness": None,
        "min_witness_count": 0,
        "max_witness_count": 0,
    }


def _item_row(connection, run_id: str) -> list:
    return connection.execute(
        "SELECT a.artifact_uid AS uid, a.dataset_id AS dataset_id, "
        "a.version_id AS version_id, a.filename AS filename, a.status AS status, "
        "a.actual_size AS actual_size, a.actual_sha256 AS actual_sha256, "
        "a.local_path AS local_path, v.released_at AS released_at, s.publisher AS publisher "
        "FROM artifacts a "
        "LEFT JOIN versions v ON v.dataset_id = a.dataset_id AND v.version_id = a.version_id "
        "LEFT JOIN dataset_publisher_snapshots s ON s.dataset_id = a.dataset_id "
        "AND s.run_id = ? "
        "WHERE a.last_run_id = ? AND a.status IN ('verified', 'skipped') "
        "ORDER BY a.artifact_uid",
        (run_id, run_id),
    ).fetchall()


def _attribute(row, registry: Registry, root: Path, variables: dict) -> dict:
    reasons: list[str] = []
    publisher = (row["publisher"] or "").strip()
    if not publisher:
        reasons.append("source_missing")
    released = row["released_at"] or ""
    if not is_day(released):
        reasons.append("invalid_date")
    if not _disk_matches(root, row):
        reasons.append("disk_mismatch")
    entry = None
    if publisher and is_day(released):
        entry = registry.resolve(publisher, released)
        if entry is None:
            reasons.append("name_not_registered")
    if reasons:
        return {"status": "unknown", "reasons": sorted(reasons)}
    key = (entry.display_name, entry.valid_from)
    if key not in variables:
        variables[key] = _Variable(entry.display_name, entry.valid_from, entry.candidates)
    weight = row["actual_size"]
    variables[key].weight += weight if isinstance(weight, int) and weight >= 0 else 0
    if len(entry.candidates) == 1:
        return {"status": "resolved", "organization": entry.candidates[0]}
    return {"status": "ambiguous", "candidates": list(entry.candidates)}


def analyze_run(connection, run_id: str, registry: Registry, root: Path) -> dict:
    """Attribute one run's accepted artifacts and bound source concentration."""
    items: list[dict] = []
    variables: dict[tuple[str, str], _Variable] = {}
    for row in _item_row(connection, run_id):
        publisher = (row["publisher"] or "").strip()
        raw_weight = row["actual_size"]
        items.append(
            {
                "artifact_uid": row["uid"],
                "dataset_id": row["dataset_id"],
                "version_id": row["version_id"],
                "filename": row["filename"],
                "status": row["status"],
                "publisher": publisher or None,
                "released_at": row["released_at"] or None,
                "weight_bytes": raw_weight if isinstance(raw_weight, int) else None,
                "attribution": _attribute(row, registry, root, variables),
            }
        )
    total_bytes = sum(item["weight_bytes"] or 0 for item in items)
    unknown_bytes = sum(
        item["weight_bytes"] or 0
        for item in items
        if item["attribution"]["status"] == "unknown"
    )
    ordered = [variables[key] for key in sorted(variables)]
    feasible: list[tuple[str, ...]] = []
    if not items or total_bytes == 0:
        status = "empty"
    elif unknown_bytes or any(item["attribution"]["status"] == "unknown" for item in items):
        status = "unknown"
    elif prod(len(variable.candidates) for variable in ordered) > MAX_ASSIGNMENTS:
        status = "underdetermined"
    else:
        feasible = [
            combo
            for combo in product(*(variable.candidates for variable in ordered))
            if _feasible(ordered, combo, registry.exclusions)
        ]
        status = "ok" if feasible else "infeasible"
    if status == "ok":
        concentration = {
            contract: _evaluate(registry, contract, ordered, feasible, total_bytes)
            for contract in CONTRACTS
        }
    else:
        concentration = {contract: _blank_contract(status) for contract in CONTRACTS}
    return {
        "schema_version": 1,
        "run_id": run_id,
        "registry": {"version": registry.version, "fingerprint": registry.fingerprint},
        "status": status,
        "totals": {
            "verified_artifacts": len(items),
            "verified_bytes": total_bytes,
            "known_bytes": total_bytes - unknown_bytes,
            "unknown_bytes": unknown_bytes,
        },
        "items": items,
        "concentration": concentration,
        "notes": [
            "All organizations, groups, and aliases are fictional registry entries.",
            "Attribution never rewrites byte acceptance, verified status, or license conclusions.",
            "Unknown evidence yields null concentration bounds, never a pseudo-precise total.",
        ],
    }
