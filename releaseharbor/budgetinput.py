"""Input loading and validation for synthetic budget adjudication.

Everything here is offline and synthetic-only: a verified ``budget_lines.csv``
(department, fiscal_year, account, amount_usd) plus a controlled local
manifest listing the fictional sensitive cells.  Every malformed input
raises :class:`BudgetError` with a stable, checkable code.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import BudgetError, ConfigError, SecurityBoundaryError
from .util import load_json_file, resolve_under, safe_filename, safe_segment

CONTRACTS = ("marginals", "details")
REQUIRED_COLUMNS = ("department", "fiscal_year", "account", "amount_usd")
MAX_CELLS = 100
MAX_YEARS = 100
MAX_AMOUNT = 10**15
_AMOUNT = re.compile(r"-?[0-9]+\Z")
_YEAR = re.compile(r"[0-9]{4}\Z")


@dataclass(frozen=True)
class SensitiveCell:
    fiscal_year: int
    department: str
    account: str

    def label(self) -> str:
        return f"{self.fiscal_year}/{self.department}/{self.account}"


@dataclass(frozen=True)
class BudgetMatrix:
    """One fiscal year's department x account non-negative integer tableau."""

    fiscal_year: int
    departments: tuple[str, ...]
    accounts: tuple[str, ...]
    values: tuple[tuple[int, ...], ...]

    def row_totals(self) -> list[int]:
        return [sum(row) for row in self.values]

    def col_totals(self) -> list[int]:
        return [sum(row[j] for row in self.values) for j in range(len(self.accounts))]

    def position(self, department: str, account: str) -> tuple[int, int] | None:
        if department not in self.departments or account not in self.accounts:
            return None
        return (self.departments.index(department), self.accounts.index(account))


def parse_budget_csv(text: str) -> list[BudgetMatrix]:
    """Parse and validate the budget CSV into per-year matrices (never mixed)."""
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
    if missing:
        raise BudgetError("missing_column", f"CSV is missing columns: {', '.join(missing)}")
    extra = [name for name in fieldnames if name not in REQUIRED_COLUMNS]
    if extra:
        raise BudgetError("unexpected_column", f"CSV has unexpected columns: {', '.join(extra)}")
    entries: dict[tuple[int, str, str], int] = {}
    for line_number, record in enumerate(reader, start=2):
        department = (record.get("department") or "").strip()
        account = (record.get("account") or "").strip()
        if not department or not account:
            raise BudgetError("empty_key", f"line {line_number}: department/account must be non-empty")
        year_text = (record.get("fiscal_year") or "").strip()
        if not _YEAR.fullmatch(year_text):
            raise BudgetError("invalid_fiscal_year", f"line {line_number}: bad year {year_text!r}")
        amount_text = (record.get("amount_usd") or "").strip()
        if not amount_text:
            raise BudgetError("missing_amount", f"line {line_number}: missing amount_usd")
        if not _AMOUNT.fullmatch(amount_text):
            raise BudgetError("invalid_amount", f"line {line_number}: non-integer {amount_text!r}")
        amount = int(amount_text)
        if amount < 0:
            raise BudgetError("negative_amount", f"line {line_number}: negative amount {amount}")
        if amount > MAX_AMOUNT:
            raise BudgetError("amount_out_of_range", f"line {line_number}: amount exceeds {MAX_AMOUNT}")
        key = (int(year_text), department, account)
        if key in entries:
            raise BudgetError(
                "duplicate_key", f"line {line_number}: duplicate {year_text}/{department}/{account}"
            )
        entries[key] = amount
    if not entries:
        raise BudgetError("empty_csv", "CSV contains no data rows")
    years: dict[int, dict[tuple[str, str], int]] = {}
    for (year, department, account), amount in entries.items():
        years.setdefault(year, {})[(department, account)] = amount
    if len(years) > MAX_YEARS:
        raise BudgetError("too_many_years", f"CSV spans more than {MAX_YEARS} fiscal years")
    matrices = []
    for year in sorted(years):
        cells = years[year]
        departments = tuple(sorted({d for d, _ in cells}))
        accounts = tuple(sorted({a for _, a in cells}))
        if len(departments) * len(accounts) > MAX_CELLS:
            raise BudgetError("matrix_too_large", f"fiscal year {year} exceeds {MAX_CELLS} cells")
        values = tuple(tuple(cells.get((d, a), 0) for a in accounts) for d in departments)
        matrices.append(BudgetMatrix(year, departments, accounts, values))
    return matrices


def load_manifest(path: Path) -> tuple[list[SensitiveCell], dict]:
    """Load the controlled local sensitive-cell manifest with strict validation."""
    try:
        raw = load_json_file(path)
    except (OSError, ValueError) as exc:
        raise BudgetError("manifest_unreadable", f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise BudgetError("manifest_invalid", "manifest must be an object with schema_version 1")
    unknown = set(raw) - {"schema_version", "sensitive_cells", "declared_totals"}
    if unknown:
        raise BudgetError("manifest_invalid", f"manifest has unknown keys: {', '.join(sorted(unknown))}")
    cells_raw = raw.get("sensitive_cells")
    if not isinstance(cells_raw, list) or not cells_raw:
        raise BudgetError("manifest_invalid", "sensitive_cells must be a non-empty list")
    cells: list[SensitiveCell] = []
    seen = set()
    for item in cells_raw:
        if not isinstance(item, dict) or set(item) != {"fiscal_year", "department", "account"}:
            raise BudgetError(
                "manifest_invalid", "sensitive cells need exactly fiscal_year, department, account"
            )
        year = item["fiscal_year"]
        if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9999:
            raise BudgetError("manifest_invalid", f"invalid manifest fiscal_year: {year!r}")
        department, account = item["department"], item["account"]
        if not all(isinstance(v, str) and v.strip() for v in (department, account)):
            raise BudgetError("manifest_invalid", "sensitive cell keys must be non-empty strings")
        cell = SensitiveCell(year, department.strip(), account.strip())
        if cell in seen:
            raise BudgetError("manifest_invalid", f"duplicate sensitive cell {cell.label()}")
        seen.add(cell)
        cells.append(cell)
    declared = raw.get("declared_totals", {})
    if not isinstance(declared, dict):
        raise BudgetError("manifest_invalid", "declared_totals must be an object")
    for year_text, totals in declared.items():
        if not isinstance(year_text, str) or not _YEAR.fullmatch(year_text):
            raise BudgetError("manifest_invalid", f"declared_totals key is not a year: {year_text!r}")
        if not isinstance(totals, dict) or set(totals) != {"rows", "cols"}:
            raise BudgetError("manifest_invalid", f"declared_totals[{year_text}] needs rows and cols")
        for section in ("rows", "cols"):
            values = totals[section]
            if not isinstance(values, dict):
                raise BudgetError("manifest_invalid", f"declared_totals[{year_text}].{section} invalid")
            for name, amount in values.items():
                if not isinstance(name, str) or not name.strip():
                    raise BudgetError("manifest_invalid", "declared total keys must be non-empty")
                if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                    raise BudgetError("manifest_invalid", f"declared total for {name!r} is invalid")
    return cells, declared


@dataclass(frozen=True)
class BudgetAdjudicationSpec:
    dataset_id: str
    version_id: str
    filename: str
    manifest: Path
    contracts: tuple[str, ...]


@dataclass(frozen=True)
class BudgetSettings:
    adjudications: tuple[BudgetAdjudicationSpec, ...]


def _config_segment(value, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")
    try:
        return safe_segment(value, label)
    except SecurityBoundaryError as exc:
        raise ConfigError(str(exc)) from exc


def parse_budget_settings(raw, root: Path) -> BudgetSettings:
    """Validate the optional ``budget`` configuration section."""
    if not isinstance(raw, dict):
        raise ConfigError("budget must be an object")
    unknown = set(raw) - {"adjudications"}
    if unknown:
        raise ConfigError(f"budget has unknown keys: {', '.join(sorted(unknown))}")
    items = raw.get("adjudications", [])
    if not isinstance(items, list) or len(items) > 20:
        raise ConfigError("budget.adjudications must be a list of at most 20 entries")
    specs = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError("budget adjudication entries must be objects")
        missing = {"dataset_id", "version_id", "filename", "manifest"} - set(item)
        unknown = set(item) - {"dataset_id", "version_id", "filename", "manifest", "contracts"}
        if missing or unknown:
            raise ConfigError(
                f"budget adjudication keys invalid: missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        dataset_id = _config_segment(item["dataset_id"], "budget.dataset_id")
        version_id = _config_segment(item["version_id"], "budget.version_id")
        try:
            filename = safe_filename(item["filename"])
        except SecurityBoundaryError as exc:
            raise ConfigError(str(exc)) from exc
        try:
            manifest = resolve_under(root, item["manifest"], "budget.manifest")
        except SecurityBoundaryError as exc:
            raise ConfigError(str(exc)) from exc
        if not manifest.is_file():
            raise ConfigError(f"budget manifest does not exist: {item['manifest']}")
        contracts_raw = item.get("contracts", list(CONTRACTS))
        if not isinstance(contracts_raw, list) or not contracts_raw:
            raise ConfigError("budget.contracts must be a non-empty list")
        if any(c not in CONTRACTS for c in contracts_raw):
            raise ConfigError(f"budget.contracts must be drawn from {list(CONTRACTS)}")
        contracts = tuple(c for c in CONTRACTS if c in set(contracts_raw))
        key = (dataset_id, version_id, filename)
        if key in seen:
            raise ConfigError(f"duplicate budget adjudication for {'/'.join(key)}")
        seen.add(key)
        specs.append(BudgetAdjudicationSpec(dataset_id, version_id, filename, manifest, contracts))
    return BudgetSettings(tuple(specs))
