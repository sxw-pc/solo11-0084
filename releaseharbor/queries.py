"""Read-only missing-object queries and declaration re-checks.

Answers are derived from a single run's coverage declarations via the
currently usable catalog view of the chosen contract.  A missing object is
only ever reported as ``absent_proven`` when the parent listing page has a
complete, stable, same-round coverage proof; every other situation is
reported as ``unknown`` (or ``filtered``) with its reason.
"""

from __future__ import annotations

import json
import sqlite3

from .catalog import (
    STATE_ABSENT,
    STATE_FILTERED,
    STATE_PRESENT,
    STATE_UNKNOWN,
    STRICT,
    _coverage_gap,
    _find_decision,
    _usable,
    current_view,
    load_declarations,
)
from .coverage import ACCEPTED, CHANNEL_FILTERED, CONFIRMED


def _answer(contract: str, view: dict, state: str, reason: str, evidence: dict | None) -> dict:
    result = {
        "contract": contract,
        "view_run_id": view["run_id"],
        "state": state,
        "reason": reason,
    }
    if evidence is not None:
        result["evidence"] = evidence
    return result


def _evidence_of(declaration: dict) -> dict:
    return {
        "page_url": declaration["page_url"],
        "content_digest": declaration["content_digest"],
        "first_fetch_seq": declaration["first_fetch_seq"],
        "verified_fetch_seq": declaration["verified_fetch_seq"],
    }


def _child_declaration(
    parent: dict, decision: dict, by_url: dict
) -> dict | str:
    child = by_url.get(decision["url"])
    if child is None:
        return "evidence_chain_broken: child declaration is missing"
    if parent["children"].get(child["page_url"]) != child["content_digest"]:
        return "evidence_chain_broken: child digest does not match the parent record"
    return child


def _walk_version(
    contract: str, view: dict, declarations: list[dict], dataset_id: str, version_id: str
) -> tuple[dict | None, dict | None, dict | None]:
    """Resolve the decision chain; returns (answer, dataset_decl, version_decl)."""
    by_url = {d["page_url"]: d for d in declarations}
    portal = next((d for d in declarations if d["level"] == "portal"), None)
    if portal is None:
        return _answer(contract, view, STATE_UNKNOWN, "no_portal_coverage", None), None, None
    if contract == STRICT and dataset_id not in view["datasets"]:
        return (
            _answer(contract, view, STATE_UNKNOWN, "outside_strict_scope", None),
            None,
            None,
        )
    decision = _find_decision(portal, dataset_id)
    if decision is None:
        if _usable(portal):
            return (
                _answer(contract, view, STATE_ABSENT, "dataset not listed on a fully read "
                        "portal page", _evidence_of(portal)),
                None,
                None,
            )
        return (
            _answer(contract, view, STATE_UNKNOWN, f"portal coverage not usable: {_coverage_gap(portal)}", None),
            None,
            None,
        )
    if decision["outcome"] != ACCEPTED:
        state = STATE_FILTERED if decision["outcome"] == CHANNEL_FILTERED else STATE_UNKNOWN
        return _answer(contract, view, state, decision["outcome"], None), None, None
    dataset = _child_declaration(portal, decision, by_url)
    if isinstance(dataset, str):
        return _answer(contract, view, STATE_UNKNOWN, dataset, None), None, None
    decision = _find_decision(dataset, version_id)
    if decision is None:
        if _usable(dataset):
            return (
                _answer(contract, view, STATE_ABSENT, "version not listed on a fully read "
                        "dataset page", _evidence_of(dataset)),
                dataset,
                None,
            )
        return (
            _answer(contract, view, STATE_UNKNOWN, f"dataset coverage not usable: {_coverage_gap(dataset)}", None),
            dataset,
            None,
        )
    if decision["outcome"] == CHANNEL_FILTERED:
        return _answer(contract, view, STATE_FILTERED, "channel_filtered", None), dataset, None
    if decision["outcome"] != ACCEPTED:
        return (
            _answer(contract, view, STATE_UNKNOWN, decision["outcome"], None),
            dataset,
            None,
        )
    version = _child_declaration(dataset, decision, by_url)
    if isinstance(version, str):
        return _answer(contract, view, STATE_UNKNOWN, version, None), dataset, None
    return None, dataset, version


def query_version(
    connection: sqlite3.Connection, contract: str, dataset_id: str, version_id: str
) -> dict:
    view = current_view(connection, contract)
    if view is None:
        return {"contract": contract, "view_run_id": None, "state": STATE_UNKNOWN,
                "reason": "no_directory"}
    declarations = load_declarations(connection, view["run_id"])
    answer, _dataset, version = _walk_version(contract, view, declarations, dataset_id, version_id)
    if answer is not None:
        return answer
    return _answer(contract, view, STATE_PRESENT, "version page was read this round",
                   _evidence_of(version))


def query_artifact(
    connection: sqlite3.Connection,
    contract: str,
    dataset_id: str,
    version_id: str,
    filename: str,
) -> dict:
    view = current_view(connection, contract)
    if view is None:
        return {"contract": contract, "view_run_id": None, "state": STATE_UNKNOWN,
                "reason": "no_directory"}
    declarations = load_declarations(connection, view["run_id"])
    answer, _dataset, version = _walk_version(contract, view, declarations, dataset_id, version_id)
    if answer is not None:
        return answer
    if contract == STRICT and version_id not in view["datasets"].get(dataset_id, {}).get(
        "versions", []
    ):
        return _answer(contract, view, STATE_UNKNOWN, "outside_strict_scope", None)
    decision = _find_decision(version, filename)
    if decision is None:
        if _usable(version):
            return _answer(
                contract, view, STATE_ABSENT,
                "artifact not listed on a fully read version page", _evidence_of(version),
            )
        return _answer(
            contract, view, STATE_UNKNOWN,
            f"version coverage not usable: {_coverage_gap(version)}", None,
        )
    if decision["outcome"] == ACCEPTED:
        return _answer(contract, view, STATE_PRESENT, "artifact listed and accepted",
                       _evidence_of(version))
    return _answer(contract, view, STATE_UNKNOWN, decision["outcome"], None)


def recheck_declaration(connection: sqlite3.Connection, run_id: str, page_url: str) -> dict:
    """Re-verify a stored declaration against the retained page evidence."""
    declarations = [
        d for d in load_declarations(connection, run_id) if d["page_url"] == page_url
    ]
    if not declarations:
        return {"ok": False, "checks": [], "reason": "declaration not found"}
    declaration = declarations[0]
    evidence = connection.execute(
        "SELECT fetch_seq, content_digest, links_json FROM page_evidence "
        "WHERE run_id = ? AND page_url = ? ORDER BY fetch_seq",
        (run_id, page_url),
    ).fetchall()
    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    first = next((e for e in evidence if e["fetch_seq"] == declaration["first_fetch_seq"]), None)
    check("first_fetch_evidence_present", first is not None)
    if first is not None:
        check("digest_matches_evidence",
              first["content_digest"] == declaration["content_digest"])
        links = json.loads(first["links_json"])
        check("candidates_match_evidence", len(links) == declaration["candidates"],
              f"evidence={len(links)} declaration={declaration['candidates']}")
        accepted = sum(1 for link in links if link["outcome"] == ACCEPTED)
        check("accepted_match_evidence", accepted == declaration["accepted"])
    for child_url, digest in declaration["children"].items():
        row = connection.execute(
            "SELECT 1 FROM page_evidence WHERE run_id = ? AND page_url = ? AND "
            "content_digest = ? LIMIT 1",
            (run_id, child_url, digest),
        ).fetchone()
        check(f"child_evidence:{child_url}", row is not None)
    if declaration["stability"] == CONFIRMED:
        row = next(
            (e for e in evidence if e["fetch_seq"] == declaration["verified_fetch_seq"]), None
        )
        check(
            "confirmation_read_matches",
            row is not None and row["content_digest"] == declaration["content_digest"],
        )
    return {"ok": all(item["ok"] for item in checks), "checks": checks,
            "declaration": declaration}
