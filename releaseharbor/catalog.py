"""Usage contracts over one run's coverage declarations.

Two independently selectable contracts share the same per-run declarations:

- ``strict``: publishes a directory usable for "missing" judgements only where
  every relevant level has a same-round, stable, complete coverage proof.
  Branches without such proof are excluded; if nothing qualifies, the run's
  strict directory is withheld and the last published strict view is retained.
- ``preview``: always publishes the partially discovered directory for viewing
  and downloading verified objects, but every uncovered branch is listed
  explicitly as ``unknown`` and no disappearance may be derived from it.

All completeness reasoning uses only declarations of a single run; page
evidence from older runs is never merged into the current round.  Read-only
missing-object queries over these views live in ``queries.py``.
"""

from __future__ import annotations

import json
import sqlite3

from .coverage import (
    ACCEPTED,
    CHANNEL_FILTERED,
    CONFIRMED,
    INCOMPLETE_OUTCOMES,
)

STRICT = "strict"
PREVIEW = "preview"
CONTRACTS = (STRICT, PREVIEW)

STATE_PRESENT = "present"
STATE_ABSENT = "absent_proven"
STATE_FILTERED = "filtered"
STATE_UNKNOWN = "unknown"

_DECISION_PRIORITY = (ACCEPTED, CHANNEL_FILTERED, "fetch_failed", "budget_skipped",
                      "invalid_link", "duplicate")


def load_declarations(connection: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = connection.execute(
        "SELECT * FROM coverage_declarations WHERE run_id = ? ORDER BY first_fetch_seq",
        (run_id,),
    ).fetchall()
    declarations = []
    for row in rows:
        declarations.append(
            {
                "level": row["level"],
                "subject": row["subject"],
                "page_url": row["page_url"],
                "parent_url": row["parent_url"],
                "content_digest": row["content_digest"],
                "first_fetch_seq": row["first_fetch_seq"],
                "verified_fetch_seq": row["verified_fetch_seq"],
                "fetched_at": row["fetched_at"],
                "stability": row["stability"],
                "complete": bool(row["complete"]),
                "candidates": row["candidates"],
                "accepted": row["accepted"],
                "decisions": json.loads(row["decisions_json"]),
                "children": json.loads(row["children_json"]),
            }
        )
    return declarations


def _usable(declaration: dict) -> bool:
    return declaration["complete"] and declaration["stability"] == CONFIRMED


def _find_decision(declaration: dict, ref: str) -> dict | None:
    matches = [d for d in declaration["decisions"] if d["ref"] == ref]
    for outcome in _DECISION_PRIORITY:
        for decision in matches:
            if decision["outcome"] == outcome:
                return decision
    return None


def reconcile(declarations: list[dict]) -> list[str]:
    """Verify the same-run parent/child digest chain; return violations."""
    violations: list[str] = []
    by_url = {d["page_url"]: d for d in declarations}
    portals = [d for d in declarations if d["level"] == "portal"]
    if len(portals) != 1:
        violations.append(f"expected exactly one portal declaration, found {len(portals)}")
    for declaration in declarations:
        if declaration["level"] != "portal":
            parent = by_url.get(declaration["parent_url"] or "")
            if parent is None:
                violations.append(f"{declaration['page_url']} lacks its parent declaration")
                continue
            recorded = parent["children"].get(declaration["page_url"])
            if recorded is None:
                violations.append(
                    f"parent {parent['page_url']} did not record child {declaration['page_url']}"
                )
            elif recorded != declaration["content_digest"]:
                violations.append(
                    f"child digest mismatch between {parent['page_url']} and "
                    f"{declaration['page_url']}"
                )
        if declaration["level"] in ("portal", "dataset"):
            for decision in declaration["decisions"]:
                if decision["outcome"] in (ACCEPTED, CHANNEL_FILTERED):
                    if decision["url"] not in by_url:
                        violations.append(
                            f"{decision['outcome']} child {decision['url']} of "
                            f"{declaration['page_url']} lacks a declaration"
                        )
    return violations


def _strict_view(declarations: list[dict], run_id: str, violations: list[str]) -> dict:
    view = {
        "contract": STRICT,
        "run_id": run_id,
        "status": "withheld",
        "reason": "",
        "datasets": {},
        "excluded": [],
        "violations": violations,
    }
    by_url = {d["page_url"]: d for d in declarations}
    portal = next((d for d in declarations if d["level"] == "portal"), None)
    if violations:
        view["reason"] = "parent/child evidence chain violations"
        return view
    if portal is None:
        view["reason"] = "no portal coverage declaration"
        return view
    if not _usable(portal):
        view["reason"] = (
            f"portal coverage is not usable "
            f"(complete={portal['complete']}, stability={portal['stability']})"
        )
        return view
    covered: dict[str, dict] = {}
    for declaration in declarations:
        if declaration["level"] != "dataset":
            continue
        subject = declaration["subject"]
        if not _usable(declaration):
            view["excluded"].append(
                {"dataset": subject, "reason": _coverage_gap(declaration)}
            )
            continue
        versions = []
        gap = ""
        for decision in declaration["decisions"]:
            if decision["outcome"] != ACCEPTED:
                continue
            child = by_url.get(decision["url"])
            if child is None or not _usable(child):
                gap = "a version lacks complete stable coverage"
                break
            versions.append(child["subject"])
        if gap:
            view["excluded"].append({"dataset": subject, "reason": gap})
            continue
        covered[subject] = {"versions": sorted(versions)}
    if not covered and portal["accepted"] > 0:
        view["reason"] = "no dataset has complete stable coverage this round"
        return view
    view["status"] = "published"
    view["datasets"] = covered
    return view


def _coverage_gap(declaration: dict) -> str:
    if not declaration["complete"]:
        outcomes = sorted(
            {d["outcome"] for d in declaration["decisions"] if d["outcome"] in INCOMPLETE_OUTCOMES}
        )
        return "incomplete: " + ",".join(outcomes)
    return f"stability_{declaration['stability']}"


def _preview_view(
    connection: sqlite3.Connection, declarations: list[dict], run_id: str, violations: list[str]
) -> dict:
    datasets: dict[str, dict] = {}
    for row in connection.execute(
        "SELECT dataset_id, title FROM datasets WHERE last_run_id = ? ORDER BY dataset_id",
        (run_id,),
    ).fetchall():
        datasets[row["dataset_id"]] = {"title": row["title"], "versions": {}}
    for row in connection.execute(
        "SELECT dataset_id, version_id, label, channel, released_at FROM versions "
        "WHERE last_run_id = ? ORDER BY dataset_id, version_id",
        (run_id,),
    ).fetchall():
        dataset = datasets.setdefault(row["dataset_id"], {"title": "", "versions": {}})
        dataset["versions"][row["version_id"]] = {
            "label": row["label"],
            "channel": row["channel"],
            "released_at": row["released_at"],
            "artifacts": {},
        }
    for row in connection.execute(
        "SELECT dataset_id, version_id, filename, status, expected_size, expected_sha256 "
        "FROM artifacts WHERE last_run_id = ? ORDER BY dataset_id, version_id, filename",
        (run_id,),
    ).fetchall():
        version = datasets.get(row["dataset_id"], {}).get("versions", {}).get(row["version_id"])
        if version is not None:
            version["artifacts"][row["filename"]] = {
                "status": row["status"],
                "expected_size": row["expected_size"],
                "expected_sha256": row["expected_sha256"],
            }
    unknowns = []
    filtered = []
    for declaration in declarations:
        if declaration["stability"] != CONFIRMED:
            unknowns.append(
                {
                    "level": declaration["level"],
                    "subject": declaration["subject"],
                    "ref": "",
                    "reason": f"stability_{declaration['stability']}",
                    "page_url": declaration["page_url"],
                }
            )
        for decision in declaration["decisions"]:
            if decision["outcome"] in INCOMPLETE_OUTCOMES:
                unknowns.append(
                    {
                        "level": declaration["level"],
                        "subject": declaration["subject"],
                        "ref": decision["ref"],
                        "reason": decision["outcome"],
                        "detail": decision["reason"],
                        "page_url": declaration["page_url"],
                    }
                )
            elif decision["outcome"] == CHANNEL_FILTERED:
                filtered.append(
                    {
                        "level": declaration["level"],
                        "subject": declaration["subject"],
                        "ref": decision["ref"],
                        "reason": decision["reason"],
                        "page_url": declaration["page_url"],
                    }
                )
    return {
        "contract": PREVIEW,
        "run_id": run_id,
        "status": "published",
        "datasets": datasets,
        "unknowns": unknowns,
        "filtered": filtered,
        "violations": violations,
    }


def evaluate_views(connection: sqlite3.Connection, run_id: str) -> dict:
    declarations = load_declarations(connection, run_id)
    violations = reconcile(declarations)
    return {
        STRICT: _strict_view(declarations, run_id, violations),
        PREVIEW: _preview_view(connection, declarations, run_id, violations),
    }


def publish_views(connection: sqlite3.Connection, ledger, run_id: str) -> dict:
    views = evaluate_views(connection, run_id)
    for contract, view in views.items():
        ledger.record_catalog_view(contract, run_id, view["status"], view)
    return views


def current_view(connection: sqlite3.Connection, contract: str) -> dict | None:
    """The currently usable directory for a contract (strict keeps the last
    complete view when the latest run was withheld)."""
    if contract == STRICT:
        row = connection.execute(
            "SELECT view_json FROM catalog_views WHERE contract = ? AND status = 'published' "
            "ORDER BY rowid DESC LIMIT 1",
            (STRICT,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT view_json FROM catalog_views WHERE contract = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (PREVIEW,),
        ).fetchone()
    return json.loads(row["view_json"]) if row else None
