"""Per-contract disclosure decisions for one synthetic budget matrix.

Each fiscal year is re-solved from scratch under one publication contract:
a bounded global enumeration finds the smallest suppression set that leaves
every sensitive cell a non-trivial feasible interval, exact min/max ranges
and two witness matrices are computed per sensitive cell, and infeasible
cases are reduced to a minimal conflicting margin set for refusal reports.
"""

from __future__ import annotations

from itertools import combinations

from .budgetflow import transport
from .budgetinput import BudgetMatrix, SensitiveCell
from .errors import BudgetError

CONTRACT_USES_COL_TOTALS = {"marginals": True, "details": False}
DEFAULT_MAX_ENUMERATION = 50_000


class _ContractView:
    """One fiscal-year matrix evaluated under one publication contract."""

    def __init__(self, matrix: BudgetMatrix, contract: str, row_totals=None, col_totals=None) -> None:
        self.matrix = matrix
        self.uses_cols = CONTRACT_USES_COL_TOTALS[contract]
        self.rows = len(matrix.departments)
        self.cols = len(matrix.accounts)
        self.row_totals = matrix.row_totals() if row_totals is None else list(row_totals)
        if col_totals is not None:
            self.col_totals = list(col_totals)
        elif self.uses_cols:
            self.col_totals = matrix.col_totals()
        else:
            self.col_totals = [None] * self.cols
        self.total = sum(matrix.row_totals())
        self.big = self.total + 1

    def relaxed(self, kept: set[tuple[str, int]]) -> "_ContractView":
        rows = [t if ("row_total", i) in kept else None for i, t in enumerate(self.matrix.row_totals())]
        cols = [t if ("col_total", j) in kept else None for j, t in enumerate(self.matrix.col_totals())]
        return _ContractView(self.matrix, "marginals" if self.uses_cols else "details", rows, cols)

    def bounds(self, free_cells: set[tuple[int, int]]) -> dict[tuple[int, int], tuple[int, int]]:
        result = {}
        for i in range(self.rows):
            for j in range(self.cols):
                if (i, j) in free_cells:
                    result[(i, j)] = (0, self.big)
                else:
                    value = self.matrix.values[i][j]
                    result[(i, j)] = (value, value)
        return result

    def feasible(self, cell_bounds) -> bool:
        return transport(self.row_totals, self.col_totals, cell_bounds) is not None

    def cell_range(self, free_cells: set[tuple[int, int]], cell: tuple[int, int]) -> tuple[int, int]:
        base = self.bounds(free_cells)
        i, j = cell
        limits = [t for t in (self.row_totals[i], self.col_totals[j]) if t is not None]
        upper = min(limits) if limits else self.total
        return (
            self._scan(base, cell, upper, find_min=True),
            self._scan(base, cell, upper, find_min=False),
        )

    def _scan(self, base, cell, upper, find_min) -> int:
        low, high, best = 0, upper, None
        while low <= high:
            mid = (low + high) // 2
            bounds = dict(base)
            bounds[cell] = (0, mid) if find_min else (mid, self.big)
            if self.feasible(bounds):
                best = mid
                low, high = (low, mid - 1) if find_min else (mid + 1, high)
            else:
                low, high = (mid + 1, high) if find_min else (low, mid - 1)
        if best is None:
            raise BudgetError("infeasible_model", "published constraints are internally infeasible")
        return best

    def witness(self, free_cells, cell, target: int) -> list[list[int]]:
        bounds = dict(self.bounds(free_cells))
        bounds[cell] = (target, target)
        flows = transport(self.row_totals, self.col_totals, bounds, want_flows=True)
        if flows is None:
            raise BudgetError("infeasible_model", f"no witness assignment for cell value {target}")
        return [[flows[(i, j)] for j in range(self.cols)] for i in range(self.rows)]


def _cell_dict(matrix: BudgetMatrix, cell: tuple[int, int]) -> dict:
    i, j = cell
    return {
        "fiscal_year": matrix.fiscal_year,
        "department": matrix.departments[i],
        "account": matrix.accounts[j],
    }


def _minimum_suppression(view: _ContractView, sensitive: set, candidates: list, max_enumeration: int):
    """Bounded global search for the smallest safe extra suppression set."""
    evaluated = 0

    def safe(extra) -> bool:
        nonlocal evaluated
        evaluated += 1
        if evaluated > max_enumeration:
            raise BudgetError(
                "search_limit_exceeded", f"suppression search exceeded {max_enumeration} evaluations"
            )
        free = sensitive | set(extra)
        return all(lo < hi for lo, hi in (view.cell_range(free, s) for s in sensitive))

    if not safe(candidates):
        return None
    for size in range(len(candidates) + 1):
        winners = [combo for combo in combinations(candidates, size) if safe(combo)]
        if winners:
            return winners[0], {
                "evaluated": evaluated,
                "minimum_extra_suppressed": size,
                "equivalent_schemes": len(winners),
            }
    return None


def _conflict_core(view: _ContractView, cell: tuple[int, int]) -> list[tuple[str, int]]:
    """Minimal published-margin subset that still pins ``cell`` (all cells free)."""
    everything = {(i, j) for i in range(view.rows) for j in range(view.cols)}
    constraints = [("row_total", i) for i in range(view.rows)]
    if view.uses_cols:
        constraints += [("col_total", j) for j in range(view.cols)]
    core = list(constraints)
    for entry in constraints:
        trial = [item for item in core if item != entry]
        lo, hi = view.relaxed(set(trial)).cell_range(everything, cell)
        if lo == hi:
            core = trial
    return core


def _constraint_labels(view: _ContractView, core: list[tuple[str, int]]) -> list[dict]:
    labels = []
    for kind, idx in core:
        if kind == "row_total":
            labels.append(
                {
                    "type": "row_total",
                    "department": view.matrix.departments[idx],
                    "value": view.matrix.row_totals()[idx],
                }
            )
        else:
            labels.append(
                {
                    "type": "col_total",
                    "account": view.matrix.accounts[idx],
                    "value": view.matrix.col_totals()[idx],
                }
            )
    return labels


def adjudicate_matrix(
    matrix: BudgetMatrix,
    sensitive_cells: list[SensitiveCell],
    contract: str,
    max_enumeration: int = DEFAULT_MAX_ENUMERATION,
) -> dict:
    """Decide one fiscal year under one contract; re-solved from scratch each call."""
    if contract not in CONTRACT_USES_COL_TOTALS:
        raise BudgetError("unknown_contract", f"unknown publication contract: {contract!r}")
    view = _ContractView(matrix, contract)
    sensitive = set()
    for cell in sensitive_cells:
        position = None if cell.fiscal_year != matrix.fiscal_year else matrix.position(
            cell.department, cell.account
        )
        if position is None:
            raise BudgetError(
                "unknown_sensitive_cell", f"{cell.label()} is not a fiscal year {matrix.fiscal_year} cell"
            )
        sensitive.add(position)
    candidates = sorted(
        (i, j) for i in range(view.rows) for j in range(view.cols) if (i, j) not in sensitive
    )
    decision = {
        "fiscal_year": matrix.fiscal_year,
        "contract": contract,
        "departments": list(matrix.departments),
        "accounts": list(matrix.accounts),
    }
    result = _minimum_suppression(view, sensitive, candidates, max_enumeration)
    if result is None:
        everything = set(candidates) | sensitive
        for cell in sorted(sensitive):
            lo, hi = view.cell_range(everything, cell)
            if lo == hi:
                decision["status"] = "refused"
                decision["conflict"] = {
                    "cell": _cell_dict(matrix, cell),
                    "constraints": _constraint_labels(view, _conflict_core(view, cell)),
                    "note": "uniquely determined by these constraints plus non-negativity",
                }
                return decision
        raise BudgetError("infeasible_model", "no safe suppression and no pinned cell")
    chosen, audit = result
    free = sensitive | set(chosen)
    decision["status"] = "publishable"
    decision["suppressed"] = [
        {
            "department": matrix.departments[i],
            "account": matrix.accounts[j],
            "sensitive": (i, j) in sensitive,
        }
        for i, j in sorted(free)
    ]
    publication = {"row_totals": dict(zip(matrix.departments, matrix.row_totals()))}
    if view.uses_cols:
        publication["col_totals"] = dict(zip(matrix.accounts, matrix.col_totals()))
    publication["cells"] = [
        {
            "department": matrix.departments[i],
            "account": matrix.accounts[j],
            "amount_usd": matrix.values[i][j],
        }
        for i in range(view.rows)
        for j in range(view.cols)
        if (i, j) not in free
    ]
    decision["publication"] = publication
    assessments = []
    for cell in sorted(sensitive):
        lo, hi = view.cell_range(free, cell)
        witnesses = [
            {
                "cell_value": target,
                "matrix": {
                    "departments": list(matrix.departments),
                    "accounts": list(matrix.accounts),
                    "values": view.witness(free, cell, target),
                },
            }
            for target in (lo, hi)
        ]
        assessments.append(
            {"cell": _cell_dict(matrix, cell), "minimum": lo, "maximum": hi, "witnesses": witnesses}
        )
    decision["assessments"] = assessments
    decision["search"] = {
        "candidate_cells": len(candidates),
        "evaluated": audit["evaluated"],
        "minimum_extra_suppressed": audit["minimum_extra_suppressed"],
        "equivalent_schemes": audit["equivalent_schemes"],
        "reason": "global minimum by bounded enumeration; equal-cost schemes ordered "
        "by stable (department, account) business key",
    }
    return decision


def _require_position(matrix: BudgetMatrix, department: str, account: str) -> tuple[int, int]:
    position = matrix.position(department, account)
    if position is None:
        raise BudgetError(
            "unknown_sensitive_cell", f"{matrix.fiscal_year}/{department}/{account} is not a matrix cell"
        )
    return position


def cell_feasible_range(
    matrix: BudgetMatrix, contract: str, suppressed: set[tuple[str, str]], cell: tuple[str, str]
) -> tuple[int, int]:
    """Feasible ``(min, max)`` for one cell given suppressed ``(department, account)`` labels."""
    view = _ContractView(matrix, contract)
    free = {_require_position(matrix, d, a) for d, a in suppressed}
    return view.cell_range(free, _require_position(matrix, *cell))


def suppression_is_safe(
    matrix: BudgetMatrix,
    contract: str,
    sensitive_labels: list[tuple[str, str]],
    suppressed: set[tuple[str, str]],
) -> bool:
    """True when every listed sensitive cell has a non-trivial feasible interval."""
    view = _ContractView(matrix, contract)
    free = {_require_position(matrix, d, a) for d, a in suppressed}
    targets = [_require_position(matrix, d, a) for d, a in sensitive_labels]
    return all(lo < hi for lo, hi in (view.cell_range(free, s) for s in targets))
