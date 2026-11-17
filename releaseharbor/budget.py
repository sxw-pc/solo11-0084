"""Offline disclosure-safety adjudication for synthetic budget cross-tabs.

A verified ``budget_lines.csv`` is cross-tabulated per fiscal year into a
non-negative integer matrix.  Publishing row/column totals next to visible
cells can let a reader solve for a suppressed sensitive cell, so every
publication contract is checked exactly: a sensitive cell is releasable
only when the published totals plus visible cells leave it a non-trivial
feasible interval under the non-negative-integer model.  Two publication
contracts are supported:

- ``marginals``: publish all department row totals and account column
  totals; suppress the fewest detail cells needed for safety.
- ``details``: publish department row totals and visible details; account
  column totals are withheld.

Suppression sets are chosen by bounded global enumeration (never a
one-shot greedy pass); equal-cost schemes are ordered by the stable
(department, account) business key.  When no safe publication exists the
decision is a refusal carrying the minimal conflicting margin constraints
and no publication fragment.  This module never processes real personal
data and never changes the source artifact's ledger status.
"""

from __future__ import annotations

from pathlib import Path

from .budgetdecide import (
    DEFAULT_MAX_ENUMERATION,
    adjudicate_matrix,
    cell_feasible_range,
    suppression_is_safe,
)
from .budgetinput import CONTRACTS, BudgetMatrix, load_manifest, parse_budget_csv

__all__ = [
    "DEFAULT_MAX_ENUMERATION",
    "adjudicate",
    "adjudicate_matrix",
    "budget_events_for_artifact",
    "budget_markdown_lines",
    "budget_report_section",
    "cell_feasible_range",
    "suppression_is_safe",
    "write_adjudication",
]
from .errors import BudgetError, IntegrityError
from .util import (
    atomic_write_text,
    canonical_json,
    relative_posix,
    safe_filename,
    safe_segment,
    sha256_bytes,
)
from .verify import require_accepted


def _check_declared_totals(by_year: dict[int, BudgetMatrix], declared_totals: dict) -> None:
    for year_text, totals in declared_totals.items():
        matrix = by_year.get(int(year_text))
        if matrix is None:
            raise BudgetError("unknown_fiscal_year", f"declared totals reference year {year_text}")
        rows, cols = totals["rows"], totals["cols"]
        if set(rows) != set(matrix.departments) or set(cols) != set(matrix.accounts):
            raise BudgetError("total_mismatch", f"declared totals for {year_text} miss matrix keys")
        for department, declared in rows.items():
            actual = matrix.row_totals()[matrix.departments.index(department)]
            if actual != declared:
                raise BudgetError(
                    "total_mismatch",
                    f"row total {department!r} in {year_text}: declared {declared}, computed {actual}",
                )
        for account, declared in cols.items():
            actual = matrix.col_totals()[matrix.accounts.index(account)]
            if actual != declared:
                raise BudgetError(
                    "total_mismatch",
                    f"column total {account!r} in {year_text}: declared {declared}, computed {actual}",
                )


def adjudicate(
    csv_path,
    expected_size: int,
    expected_sha256: str,
    manifest_path,
    contracts=CONTRACTS,
    max_enumeration: int = DEFAULT_MAX_ENUMERATION,
) -> dict:
    """Adjudicate a verified-on-disk budget CSV against the local manifest."""
    csv_path = Path(csv_path)
    try:
        integrity = require_accepted(csv_path, expected_size, expected_sha256)
    except IntegrityError as exc:
        raise BudgetError("source_reverify_failed", exc.detail) from exc
    raw_bytes = csv_path.read_bytes()
    if len(raw_bytes) != integrity.actual_size or sha256_bytes(raw_bytes) != integrity.actual_sha256:
        raise BudgetError("source_byte_drift", "source bytes changed during adjudication")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BudgetError("encoding_error", f"CSV is not valid UTF-8: {exc}") from exc
    unknown = set(contracts) - set(CONTRACTS)
    if unknown:
        raise BudgetError("unknown_contract", f"unknown publication contract: {sorted(unknown)[0]!r}")
    wanted = [c for c in CONTRACTS if c in set(contracts)]
    matrices = parse_budget_csv(text)
    sensitive_cells, declared_totals = load_manifest(Path(manifest_path))
    by_year = {matrix.fiscal_year: matrix for matrix in matrices}
    for cell in sensitive_cells:
        matrix = by_year.get(cell.fiscal_year)
        if matrix is None or matrix.position(cell.department, cell.account) is None:
            raise BudgetError("unknown_sensitive_cell", f"{cell.label()} is not present in the CSV")
    _check_declared_totals(by_year, declared_totals)
    decisions = [
        adjudicate_matrix(
            matrix,
            [c for c in sensitive_cells if c.fiscal_year == matrix.fiscal_year],
            contract,
            max_enumeration,
        )
        for matrix in matrices
        for contract in wanted
    ]
    return {
        "schema_version": 1,
        "kind": "budget_adjudication",
        "synthetic_data_only": True,
        "source": {
            "filename": csv_path.name,
            "size": integrity.actual_size,
            "sha256": integrity.actual_sha256,
        },
        "contracts": wanted,
        "decisions": decisions,
        "steward_note": "steward-side audit artifact; only the per-decision "
        "'publication' blocks are releasable",
    }


def write_adjudication(report: dict, path: Path) -> None:
    atomic_write_text(path, canonical_json(report) + "\n")


def budget_events_for_artifact(config, spec, path, expected_size, expected_sha256, base_detail):
    """Adjudicate one accepted artifact and return ``(code, level, detail)`` events.

    Never raises :class:`BudgetError`; failures become ``budget.error`` events
    so the artifact's ledger status is never affected.
    """
    try:
        report = adjudicate(
            path, expected_size, expected_sha256, spec.manifest, contracts=spec.contracts
        )
    except BudgetError as exc:
        return [("budget.error", "warning", {**base_detail, "code": exc.code, "detail": exc.detail})]
    target = (
        config.download.report_dir
        / "budget"
        / safe_segment(spec.dataset_id, "dataset id")
        / safe_segment(spec.version_id, "version id")
        / (safe_filename(spec.filename) + ".adjudication.json")
    )
    write_adjudication(report, target)
    relative = relative_posix(config.root, target)
    events = []
    for decision in report["decisions"]:
        detail = {
            **base_detail,
            "fiscal_year": decision["fiscal_year"],
            "contract": decision["contract"],
            "status": decision["status"],
            "report": relative,
        }
        if decision["status"] == "publishable":
            detail["suppressed"] = [
                f"{cell['department']}/{cell['account']}" for cell in decision["suppressed"]
            ]
            events.append(("budget.adjudicated", "info", detail))
        else:
            detail["conflict"] = decision["conflict"]["constraints"]
            events.append(("budget.refused", "warning", detail))
    return events


def budget_report_section(events: list[dict]) -> list[dict]:
    """Summarize budget adjudication audit events for the JSON report."""
    items = []
    for event in events:
        if event["code"] not in {"budget.adjudicated", "budget.refused", "budget.error"}:
            continue
        detail = event["detail"]
        entry = {
            "seq": event["seq"],
            "code": event["code"],
            "dataset": detail.get("dataset"),
            "version": detail.get("version"),
            "filename": detail.get("filename"),
            "fiscal_year": detail.get("fiscal_year"),
            "contract": detail.get("contract"),
            "status": detail.get("status", "error"),
            "report": detail.get("report"),
        }
        if "suppressed" in detail:
            entry["suppressed"] = detail["suppressed"]
        if "conflict" in detail:
            entry["conflict"] = detail["conflict"]
        if "code" in detail:
            entry["error"] = {"code": detail["code"], "detail": detail["detail"]}
        items.append(entry)
    return items


def budget_markdown_lines(items: list[dict]) -> list[str]:
    """Render the budget adjudication section of the Markdown report."""
    lines = [
        "## Budget disclosure adjudication (synthetic fixtures)",
        "",
        "| Dataset | Version | Artifact | Fiscal year | Contract | Status | Suppressed cells |",
        "|---|---|---|---|---|---|---:|",
    ]
    for item in items:
        lines.append(
            f"| {item['dataset']} | {item['version']} | {item['filename']} | "
            f"{item.get('fiscal_year') or ''} | {item.get('contract') or ''} | "
            f"{item['status']} | {len(item.get('suppressed', []))} |"
        )
    lines.extend(
        [
            "",
            "> Adjudication details live in the steward-side JSON referenced by each "
            "`budget.*` audit event; refused contracts publish no table fragments.",
            "",
        ]
    )
    return lines
