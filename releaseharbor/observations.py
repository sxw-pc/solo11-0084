"""Exact-decimal parsing and row-level adjudication for paired observation files.

The module is offline and side-effect free.  It receives two already
byte-verified payloads (one CSV, one JSONL), never uses binary floats, and
answers two independent questions:

``observation_audit``
    Does every ``(site_id, observed_on)`` group admit one global one-to-one
    pairing of physical CSV rows to physical JSONL rows in which *both* the
    pm25 and pm10 measurement intervals overlap?

``daily_summary``
    Are the per-group observation counts equal, and are the exact total
    measurement intervals compatible?  This is an aggregate-only contract and
    is explicitly not evidence of row-level equality.

Numbers are parsed as :class:`decimal.Decimal` from their lexical form.  The
place value of the last shown digit defines a half-unit measurement interval::

    8     -> [7.5, 8.5]
    8.0   -> [7.95, 8.05]
    8.50  -> [8.495, 8.505]

A physical row may be used at most once in a group; the match is a single
global bipartite matching, never two independent per-column pairings.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from .util import canonical_json, sha256_bytes
from .verify import verify_bytes

CSV_MEDIA_TYPE = "text/csv"
JSONL_MEDIA_TYPE = "application/x-ndjson"
REQUIRED_FIELDS = ("site_id", "observed_on", "pm25", "pm10")
PM_FIELDS = ("pm25", "pm10")
EXPECTED_UNITS = {"pm25": "ug/m3", "pm10": "ug/m3"}

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

CONTRACT_AUDIT = "observation_audit"
CONTRACT_SUMMARY = "daily_summary"

AUDIT_NOTICE = (
    "Requires one global physical-row bijection per (site_id, observed_on) "
    "group with overlapping pm25 and pm10 measurement intervals. Completed "
    "file order is never used to decide a winner."
)
SUMMARY_NOTICE = (
    "Aggregate-only contract: equal observation counts and compatible exact "
    "total measurement intervals per (site_id, observed_on) group. Individual "
    "rows may be recombined; this verdict MUST NOT be used for row-level audit."
)

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PRECISION_DIRECTIVE = re.compile(
    r"#\s*(?:releaseharbor[-_ ])?precision\s*:?\s*(.*)\Z", re.IGNORECASE
)
_PRECISION_TOKEN = re.compile(r"(pm25|pm10)\s*=\s*(\d{1,2})\Z", re.IGNORECASE)
_UNIT_ALIASES = {"ug/m3": "ug/m3", "ugm3": "ug/m3", "micrograms/m3": "ug/m3"}
_DECIMAL_CONTEXT = 200  # comfortably above any accepted token width
_MAX_TOKEN_DIGITS = 40
# Bounded work: matching is super-linear in group size and the synthetic
# fixture has three rows per group.  A larger group is reported as unknown
# rather than tying up the report writer; the limit is far above any realistic
# fixture while keeping worst-case runtime well under a second per group.
MAX_GROUP_ROWS = 256
_ENUMERATION_LIMIT = 24     # rows per side above which only one witness is listed
_ENUMERATION_RESULTS = 8    # maximum number of alternative witnesses listed


# ---------------------------------------------------------------------------
# Issues and source evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One undecidable source condition, anchored to a physical line."""

    source: str
    line: int | None
    code: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "line": self.line,
            "code": self.code,
            "detail": dict(self.detail),
        }


@dataclass
class ArtifactSource:
    """A release artifact plus the result of re-verifying its current bytes."""

    kind: str
    filename: str
    media_type: str
    status: str
    expected_size: int
    expected_sha256: str
    observed_size: int | None = None
    observed_sha256: str | None = None
    text: str | None = None
    issues: list[Issue] = field(default_factory=list)

    def evidence(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "status": self.status,
            "expected": {"size": self.expected_size, "sha256": self.expected_sha256},
            "observed": {"size": self.observed_size, "sha256": self.observed_sha256},
        }


def load_artifact_source(
    *,
    kind: str,
    filename: str,
    media_type: str,
    status: str,
    expected_size: int,
    expected_sha256: str,
    path: Path | None,
) -> ArtifactSource:
    """Re-read and re-hash an artifact from disk before trusting its content.

    Returns an :class:`ArtifactSource` whose ``issues`` explain why the bytes
    cannot be interpreted.  The caller must treat any issue as "unknown" for
    both contracts; a previously accepted artifact is never assumed current.
    """

    source = ArtifactSource(
        kind=kind,
        filename=filename,
        media_type=media_type,
        status=status,
        expected_size=expected_size,
        expected_sha256=(expected_sha256 or "").lower(),
    )
    expected_media = CSV_MEDIA_TYPE if kind == "csv" else JSONL_MEDIA_TYPE
    if media_type != expected_media:
        source.issues.append(
            Issue(
                kind,
                None,
                "media_type_mismatch",
                {"expected": expected_media, "observed": media_type},
            )
        )
    if status not in {"verified", "skipped"}:
        source.issues.append(
            Issue(
                kind,
                None,
                "artifact_not_accepted",
                {"status": status, "reason": "release acceptance did not verify this file"},
            )
        )
        return source
    if path is None or not path.is_file():
        source.issues.append(
            Issue(kind, None, "artifact_missing", {"path": None if path is None else str(path)})
        )
        return source
    try:
        data = path.read_bytes()
    except OSError as exc:
        source.issues.append(Issue(kind, None, "artifact_unreadable", {"reason": str(exc)}))
        return source
    report = verify_bytes(data, expected_size, expected_sha256)
    source.observed_size = report.actual_size
    source.observed_sha256 = report.actual_sha256
    if not report.accepted:
        source.issues.append(
            Issue(
                kind,
                None,
                "artifact_tampered",
                {
                    "expected_size": expected_size,
                    "observed_size": report.actual_size,
                    "expected_sha256": expected_sha256.lower(),
                    "observed_sha256": report.actual_sha256,
                },
            )
        )
        return source
    try:
        source.text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        source.issues.append(
            Issue(kind, None, "decode_error", {"reason": str(exc), "encoding": "utf-8"})
        )
    return source


# ---------------------------------------------------------------------------
# Precision rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrecisionRule:
    """Optional explicit per-metric decimal places overriding file text.

    ``None`` means "use what the file declares, else the shown last digit".
    Changing this rule changes the precision fingerprint, so verdicts are
    recomputed rather than borrowed from an earlier rule.
    """

    pm25: int | None = None
    pm10: int | None = None

    def __post_init__(self) -> None:
        for name, value in (("pm25", self.pm25), ("pm10", self.pm10)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12
            ):
                raise ValueError(f"precision.{name} must be an integer in [0, 12]")

    def as_dict(self) -> dict[str, Any]:
        return {"pm25": self.pm25, "pm10": self.pm10}

    def explicit(self, metric: str) -> int | None:
        return self.pm25 if metric == "pm25" else self.pm10


DEFAULT_PRECISION = PrecisionRule()


def _decimal_str(value: Decimal) -> str:
    return format(value, "f")


def _measurement_interval(value: Decimal, places: int) -> tuple[Decimal, Decimal]:
    with localcontext() as context:
        context.prec = _DECIMAL_CONTEXT
        half = Decimal(5).scaleb(-(places + 1))
        return value - half, value + half


def _overlaps(left: tuple[Decimal, Decimal], right: tuple[Decimal, Decimal]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _decimal_from_text(token: str) -> Decimal | None:
    text = token.strip()
    if not _NUMBER.fullmatch(text):
        return None
    digits = sum(char.isdigit() for char in text)
    if digits > _MAX_TOKEN_DIGITS:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _shown_places(token: str) -> int:
    text = token.strip().lstrip("+-")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _canonical_unit(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = (
        value.strip()
        .lower()
        .replace("µ", "u")
        .replace("μ", "u")
        .replace("³", "3")
        .replace(" ", "")
    )
    return _UNIT_ALIASES.get(text)


def _validate_date(value: Any) -> str | None:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parsed observations
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    source: str
    line: int
    site_id: str
    observed_on: str
    values: dict[str, Decimal]
    shown_places: dict[str, int]
    units: dict[str, str]

    @property
    def key(self) -> tuple[str, str]:
        return (self.site_id, self.observed_on)


@dataclass
class ParsedObservations:
    kind: str
    rows: list[Observation] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    declared: dict[str, int] = field(default_factory=dict)
    declared_line: int | None = None

    def effective_places(self, rule: PrecisionRule, metric: str) -> tuple[str, int | None]:
        explicit = rule.explicit(metric)
        if explicit is not None:
            return ("explicit", explicit)
        if metric in self.declared:
            return ("declared", self.declared[metric])
        return ("shown", None)


def _parse_precision_tokens(
    source: str, line: int, text: str
) -> tuple[dict[str, int], list[Issue]]:
    places: dict[str, int] = {}
    issues: list[Issue] = []
    tokens = [item for item in re.split(r"[,\s]+", text.strip()) if item]
    if not tokens:
        return places, [
            Issue(source, line, "precision_declaration", {"reason": "empty declaration"})
        ]
    for token in tokens:
        match = _PRECISION_TOKEN.fullmatch(token)
        if match is None:
            issues.append(
                Issue(
                    source,
                    line,
                    "precision_declaration",
                    {"token": token, "reason": "expected pm25=<places> or pm10=<places>"},
                )
            )
            continue
        metric = match.group(1).lower()
        if metric in places:
            issues.append(
                Issue(
                    source,
                    line,
                    "precision_declaration",
                    {"metric": metric, "reason": "duplicate"},
                )
            )
            continue
        places[metric] = int(match.group(2))
    return places, issues


def _make_observation(
    source: str,
    line: int,
    site_raw: Any,
    date_raw: Any,
    metric_values: dict[str, Any],
    shown_places: dict[str, int],
    units: dict[str, str],
) -> tuple[Observation | None, list[Issue]]:
    issues: list[Issue] = []
    if not isinstance(site_raw, str) or not site_raw.strip():
        issues.append(Issue(source, line, "missing_field", {"field": "site_id"}))
        site_id = ""
    else:
        site_id = site_raw.strip()
    if not isinstance(date_raw, str) or not date_raw.strip():
        issues.append(Issue(source, line, "missing_field", {"field": "observed_on"}))
        observed_on = None
    else:
        observed_on = _validate_date(date_raw)
        if observed_on is None:
            issues.append(
                Issue(
                    source, line, "invalid_date", {"field": "observed_on", "value": date_raw}
                )
            )
    values: dict[str, Decimal] = {}
    for metric, raw in ((name, metric_values.get(name)) for name in PM_FIELDS):
        if raw is None:
            issues.append(Issue(source, line, "missing_field", {"field": metric}))
            continue
        if not isinstance(raw, Decimal):
            if isinstance(raw, str) and raw.strip().upper().lstrip("+-") in {
                "NAN",
                "INF",
                "INFINITY",
            }:
                issues.append(
                    Issue(source, line, "non_finite_number", {"field": metric, "value": raw})
                )
            else:
                issues.append(
                    Issue(
                        source,
                        line,
                        "invalid_number",
                        {"field": metric, "value": raw if isinstance(raw, str) else repr(raw)},
                    )
                )
            continue
        if not raw.is_finite():
            issues.append(
                Issue(source, line, "non_finite_number", {"field": metric, "value": str(raw)})
            )
            continue
        values[metric] = raw
    for metric, unit in units.items():
        expected = EXPECTED_UNITS[metric]
        if unit != expected:
            issues.append(
                Issue(
                    source,
                    line,
                    "unit_mismatch",
                    {"field": metric, "expected": expected, "observed": unit},
                )
            )
    if len(values) == 2 and values["pm25"] > values["pm10"]:
        issues.append(
            Issue(
                source,
                line,
                "pm_order",
                {
                    "pm25": _decimal_str(values["pm25"]),
                    "pm10": _decimal_str(values["pm10"]),
                    "reason": "pm25 must not exceed pm10",
                },
            )
        )
    if issues:
        return None, issues
    return (
        Observation(
            source=source,
            line=line,
            site_id=site_id,
            observed_on=observed_on,
            values=values,
            shown_places=shown_places,
            units=units,
        ),
        [],
    )


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def parse_csv(text: str) -> ParsedObservations:
    parsed = ParsedObservations(kind="csv")
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text, newline=""))
    header: list[str] | None = None
    previous_line = 0
    try:
        for cells in reader:
            start_line = previous_line + 1
            previous_line = reader.line_num
            if not cells or all(not cell.strip() for cell in cells):
                continue
            first = cells[0].strip()
            if first.startswith("#"):
                directive = _PRECISION_DIRECTIVE.fullmatch(first)
                if directive is None:
                    continue  # ordinary comment line
                if header is not None:
                    parsed.issues.append(
                        Issue(
                            "csv",
                            start_line,
                            "precision_declaration",
                            {"reason": "after header"},
                        )
                    )
                    continue
                if parsed.declared_line is not None:
                    parsed.issues.append(
                        Issue(
                            "csv",
                            start_line,
                            "precision_declaration",
                            {"reason": "duplicate declaration"},
                        )
                    )
                    continue
                places, issues = _parse_precision_tokens("csv", start_line, directive.group(1))
                parsed.issues.extend(issues)
                parsed.declared = places
                parsed.declared_line = start_line
                continue
            if header is None:
                header = [cell.strip().lower() for cell in cells]
                if len(set(header)) != len(header):
                    parsed.issues.append(
                        Issue("csv", start_line, "csv_header", {"reason": "duplicate column"})
                    )
                    return parsed
                missing = [name for name in REQUIRED_FIELDS if name not in header]
                if missing:
                    parsed.issues.append(
                        Issue("csv", start_line, "csv_header", {"missing": missing})
                    )
                    return parsed
                continue
            if len(cells) != len(header):
                parsed.issues.append(
                    Issue(
                        "csv",
                        start_line,
                        "row_length",
                        {"expected": len(header), "observed": len(cells)},
                    )
                )
                continue
            fields = {name: cells[index] for index, name in enumerate(header)}
            shown: dict[str, int] = {}
            numbers: dict[str, Decimal | None] = {}
            for metric in PM_FIELDS:
                raw = fields.get(metric)
                if raw is None or not raw.strip():
                    numbers[metric] = None
                    continue
                value = _decimal_from_text(raw)
                if value is None:
                    numbers[metric] = raw.strip()  # keep the bad token for the issue
                    continue
                numbers[metric] = value
                shown[metric] = _shown_places(raw)
            units: dict[str, str] = {}
            general_unit = fields.get("unit")
            for metric in PM_FIELDS:
                raw_unit = fields.get(f"{metric}_unit", general_unit)
                if raw_unit is not None and raw_unit.strip():
                    canonical = _canonical_unit(raw_unit)
                    units[metric] = canonical if canonical is not None else raw_unit.strip()
            observation, issues = _make_observation(
                "csv",
                start_line,
                fields.get("site_id"),
                fields.get("observed_on"),
                numbers,
                shown,
                units,
            )
            parsed.issues.extend(issues)
            if observation is not None:
                parsed.rows.append(observation)
    except csv.Error as exc:
        parsed.issues.append(
            Issue("csv", reader.line_num or None, "csv_structure", {"reason": str(exc)})
        )
    if header is None and not parsed.issues:
        parsed.issues.append(Issue("csv", None, "csv_header", {"reason": "missing header"}))
    return parsed


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


class _DuplicateKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate JSON key: {key}")
        self.key = key


class _NonFinite(ValueError):
    pass


def _reject_constant(token: str) -> Any:
    raise _NonFinite(token)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _precision_from_object(
    source: str, line: int, payload: Any
) -> tuple[dict[str, int], list[Issue]]:
    if not isinstance(payload, dict):
        return {}, [Issue(source, line, "precision_declaration", {"reason": "must be an object"})]
    unknown = set(payload) - set(PM_FIELDS)
    if unknown:
        return {}, [
            Issue(source, line, "precision_declaration", {"unknown": sorted(unknown)})
        ]
    places: dict[str, int] = {}
    for metric, value in payload.items():
        # JSON integers are parsed as Decimal so observation numbers keep their
        # lexical precision; an integral Decimal is still a valid place count.
        if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12:
            return {}, [
                Issue(
                    source,
                    line,
                    "precision_declaration",
                    {"metric": metric, "value": repr(value)},
                )
            ]
        places[metric] = value
    return places, []


def parse_jsonl(text: str) -> ParsedObservations:
    parsed = ParsedObservations(kind="jsonl")
    if text.startswith("\ufeff"):
        text = text[1:]
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(
                line,
                parse_float=Decimal,
                parse_int=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except _DuplicateKey as exc:
            parsed.issues.append(
                Issue("jsonl", line_number, "duplicate_json_key", {"key": exc.key})
            )
            continue
        except _NonFinite as exc:
            parsed.issues.append(
                Issue("jsonl", line_number, "non_finite_number", {"token": str(exc)})
            )
            continue
        except (ValueError, json.JSONDecodeError) as exc:
            parsed.issues.append(
                Issue("jsonl", line_number, "json_structure", {"reason": str(exc)})
            )
            continue
        if not isinstance(obj, dict):
            parsed.issues.append(
                Issue("jsonl", line_number, "json_structure", {"reason": "expected an object"})
            )
            continue
        if set(obj) == {"_precision"}:
            if parsed.declared_line is not None:
                parsed.issues.append(
                    Issue(
                        "jsonl",
                        line_number,
                        "precision_declaration",
                        {"reason": "duplicate declaration"},
                    )
                )
                continue
            places, issues = _precision_from_object("jsonl", line_number, obj["_precision"])
            parsed.issues.extend(issues)
            parsed.declared = places
            parsed.declared_line = line_number
            continue

        shown: dict[str, int] = {}
        values: dict[str, Decimal | None] = {}
        for metric in PM_FIELDS:
            value = obj.get(metric)
            if value is None:
                values[metric] = None
                continue
            if isinstance(value, Decimal):
                if len(value.as_tuple().digits) > _MAX_TOKEN_DIGITS:
                    parsed.issues.append(
                        Issue(
                            "jsonl",
                            line_number,
                            "invalid_number",
                            {"field": metric, "reason": "numeric token too wide"},
                        )
                    )
                    values[metric] = None
                    continue
                values[metric] = value
                shown[metric] = max(0, -value.as_tuple().exponent)  # type: ignore[arg-type]
            else:
                values[metric] = value  # type: ignore[assignment]
        units: dict[str, str] = {}
        general_unit = obj.get("unit")
        for metric in PM_FIELDS:
            raw_unit = obj.get(f"{metric}_unit", general_unit)
            if raw_unit is not None:
                canonical = _canonical_unit(raw_unit)
                units[metric] = canonical if canonical is not None else str(raw_unit)
        observation, issues = _make_observation(
            "jsonl",
            line_number,
            obj.get("site_id"),
            obj.get("observed_on"),
            values,
            shown,
            units,
        )
        parsed.issues.extend(issues)
        if observation is not None:
            parsed.rows.append(observation)
    return parsed


# ---------------------------------------------------------------------------
# Grouped structures
# ---------------------------------------------------------------------------


@dataclass
class GroupView:
    key: tuple[str, str]
    csv_rows: list[Observation] = field(default_factory=list)
    json_rows: list[Observation] = field(default_factory=list)


@dataclass
class Pairing:
    """Witness for one global row-level matching."""

    csv_line: int
    json_line: int
    csv_pm25: tuple[str, str]
    csv_pm10: tuple[str, str]
    json_pm25: tuple[str, str]
    json_pm10: tuple[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "csv_line": self.csv_line,
            "json_line": self.json_line,
            "csv": {"pm25": list(self.csv_pm25), "pm10": list(self.csv_pm10)},
            "json": {"pm25": list(self.json_pm25), "pm10": list(self.json_pm10)},
        }


@dataclass
class GroupResult:
    site_id: str
    observed_on: str
    csv_rows: list[int]
    json_rows: list[int]
    audit: str
    summary: str
    pairing: list[Pairing] = field(default_factory=list)
    unmatched_csv: list[int] = field(default_factory=list)
    unmatched_json: list[int] = field(default_factory=list)
    duplicate_rows: list[dict[str, Any]] = field(default_factory=list)
    witness_kind: str | None = None
    witness_sources: dict[str, list[int]] = field(default_factory=dict)
    alternate_witnesses: list[dict[str, Any]] = field(default_factory=list)
    minimum_removal_size: int | None = None
    audit_reasons: list[str] = field(default_factory=list)
    csv_total_pm25: tuple[str, str] | None = None
    csv_total_pm10: tuple[str, str] | None = None
    json_total_pm25: tuple[str, str] | None = None
    json_total_pm10: tuple[str, str] | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self, contract: str | None = None) -> dict[str, Any]:
        """Project the group evidence appropriate to one contract.

        The row-level details are deliberately omitted from the aggregate
        contract, so a dashboard consumer cannot mistake an aggregate pass
        for a statement about individual rows.
        """

        totals = {
            "csv": {"pm25": _pair(self.csv_total_pm25), "pm10": _pair(self.csv_total_pm10)},
            "json": {"pm25": _pair(self.json_total_pm25), "pm10": _pair(self.json_total_pm10)},
        }
        key = {
            "site_id": self.site_id,
            "observed_on": self.observed_on,
            "csv_lines": list(self.csv_rows),
            "json_lines": list(self.json_rows),
        }
        if contract == CONTRACT_SUMMARY:
            return {
                **key,
                "csv_count": len(self.csv_rows),
                "json_count": len(self.json_rows),
                "summary": self.summary,
                "totals": totals,
                "reasons": list(self.reasons),
            }
        return {
            **key,
            "audit": self.audit,
            "pairing": [item.as_dict() for item in self.pairing],
            "unmatched_csv_lines": list(self.unmatched_csv),
            "unmatched_json_lines": list(self.unmatched_json),
            "duplicate_rows": list(self.duplicate_rows),
            "witness_kind": self.witness_kind,
            "witness_sources": {
                name: list(lines) for name, lines in sorted(self.witness_sources.items())
            },
            "minimum_removal_size": self.minimum_removal_size,
            "alternate_witnesses": list(self.alternate_witnesses),
            "audit_reasons": list(self.audit_reasons),
        }


def _pair(interval: tuple[str, str] | None) -> list[str] | None:
    return None if interval is None else [interval[0], interval[1]]


def _row_signature(row: Observation) -> str:
    return canonical_json(
        {
            "site_id": row.site_id,
            "observed_on": row.observed_on,
            "pm25": _decimal_str(row.values["pm25"]),
            "pm10": _decimal_str(row.values["pm10"]),
        }
    )


def _interval_strings(row: Observation, metric: str, places: int) -> tuple[str, str]:
    low, high = _measurement_interval(row.values[metric], places)
    return (_decimal_str(low), _decimal_str(high))


def _row_places(
    row: Observation, rule: PrecisionRule, metric: str, declared: dict[str, int]
) -> int:
    explicit = rule.explicit(metric)
    if explicit is not None:
        return explicit
    if metric in declared:
        return declared[metric]
    return row.shown_places.get(metric, 0)


def _compatible(
    left: Observation,
    right: Observation,
    rule: PrecisionRule,
    declared_csv: dict[str, int],
    declared_json: dict[str, int],
) -> bool:
    for metric in PM_FIELDS:
        left_interval = _measurement_interval(
            left.values[metric], _row_places(left, rule, metric, declared_csv)
        )
        right_interval = _measurement_interval(
            right.values[metric], _row_places(right, rule, metric, declared_json)
        )
        if not _overlaps(left_interval, right_interval):
            return False
    return True


def _match_graph(
    adjacency: list[list[int]],
    order: list[int] | None = None,
) -> list[int]:
    """Maximum bipartite matching by iterative Kuhn augmenting paths.

    Iterative rather than recursive so a group at :data:`MAX_GROUP_ROWS`
    cannot exhaust the interpreter stack.  Candidate lists are pre-sorted by
    caller-supplied content order, so the result is deterministic.
    """

    left_count = len(adjacency)
    if order is None:
        order = list(range(left_count))
    match_left = [-1] * left_count
    match_right: dict[int, int] = {}
    for start in order:
        if match_left[start] != -1:
            continue
        stack: list[tuple[int, int]] = [(start, 0)]
        parent: dict[int, int] = {}
        seen_right: set[int] = set()
        free_right: int | None = None
        while stack:
            left_index, cursor = stack[-1]
            if cursor >= len(adjacency[left_index]):
                stack.pop()
                continue
            stack[-1] = (left_index, cursor + 1)
            right_index = adjacency[left_index][cursor]
            if right_index in seen_right:
                continue
            seen_right.add(right_index)
            parent[right_index] = left_index
            if right_index not in match_right:
                free_right = right_index
                break
            stack.append((match_right[right_index], 0))
        if free_right is None:
            continue
        right_index = free_right
        while right_index != -1:
            left_index = parent[right_index]
            previous = match_left[left_index]
            match_left[left_index] = right_index
            match_right[right_index] = left_index
            right_index = previous
    return match_left


def _augmenting_match(
    left: list[Observation],
    right: list[Observation],
    rule: PrecisionRule,
    declared_csv: dict[str, int],
    declared_json: dict[str, int],
) -> list[int]:
    """Maximum matching over rows, explored in canonical content order.

    Edges are explored by sorting *content* signatures, so the matching is a
    property of the two multisets rather than of physical row order.
    """

    adjacency = _compatibility_graph(
        left, right, rule, declared_csv, declared_json
    )
    for candidates in adjacency:
        candidates.sort(
            key=lambda index: (_row_signature(right[index]), right[index].line)
        )
    order = sorted(
        range(len(left)),
        key=lambda index: (_row_signature(left[index]), left[index].line),
    )
    return _match_graph(adjacency, order)


def _pairing_from_assignment(
    csv_rows: list[Observation],
    json_rows: list[Observation],
    assignment: list[int],
    rule: PrecisionRule,
    declared_csv: dict[str, int],
    declared_json: dict[str, int],
) -> list[Pairing]:
    pairs = []
    for index, match in enumerate(assignment):
        if match < 0:
            continue
        csv_row = csv_rows[index]
        json_row = json_rows[match]
        pairs.append(
            Pairing(
                csv_line=csv_row.line,
                json_line=json_row.line,
                csv_pm25=_interval_strings(
                    csv_row, "pm25", _row_places(csv_row, rule, "pm25", declared_csv)
                ),
                csv_pm10=_interval_strings(
                    csv_row, "pm10", _row_places(csv_row, rule, "pm10", declared_csv)
                ),
                json_pm25=_interval_strings(
                    json_row, "pm25", _row_places(json_row, rule, "pm25", declared_json)
                ),
                json_pm10=_interval_strings(
                    json_row, "pm10", _row_places(json_row, rule, "pm10", declared_json)
                ),
            )
        )
    return sorted(pairs, key=lambda item: (item.csv_line, item.json_line))


def _compatibility_graph(
    csv_rows: list[Observation],
    json_rows: list[Observation],
    rule: PrecisionRule,
    declared_csv: dict[str, int],
    declared_json: dict[str, int],
) -> list[list[int]]:
    return [
        [
            index
            for index, json_row in enumerate(json_rows)
            if _compatible(csv_row, json_row, rule, declared_csv, declared_json)
        ]
        for csv_row in csv_rows
    ]


def _removal_witnesses(
    csv_rows: list[Observation],
    json_rows: list[Observation],
    rule: PrecisionRule,
    declared_csv: dict[str, int],
    declared_json: dict[str, int],
) -> list[dict[str, Any]]:
    """Minimum deletion sets, proven optimal by the deficiency bound.

    With maximum matching size ``M``, a deletion set ``D`` that leaves a
    perfect matching of size ``k`` satisfies ``k <= M`` because every matching
    of a subgraph is a matching of the whole graph, so
    ``|D| = n_csv + n_json - 2k >= n_csv + n_json - 2M``.  Deleting exactly
    the vertices left unmatched by one maximum matching attains that bound and
    is therefore minimum.  Different maximum matchings can yield different
    minimum sets; each is emitted with its own constructed pairing so the
    report never claims a single set is the only possible one.

    Witnesses are ordered by canonical row content (site, date, value pair),
    not by physical position, so the primary witness is stable under file
    reordering.
    """

    graph = _compatibility_graph(
        csv_rows, json_rows, rule, declared_csv, declared_json
    )
    seen: set[tuple[tuple[str, ...], ...]] = set()
    witnesses: list[dict[str, Any]] = []
    for variant in _maximum_matchings(graph, len(csv_rows)):
        used_json = {match for match in variant if match >= 0}
        unmatched_csv = [
            row.line for index, row in enumerate(csv_rows) if variant[index] < 0
        ]
        unmatched_json = [
            row.line for index, row in enumerate(json_rows) if index not in used_json
        ]
        pairing = _pairing_from_assignment(
            csv_rows, json_rows, variant, rule, declared_csv, declared_json
        )
        canonical = (
            tuple(sorted(_row_signature(csv_rows[index]) for index in range(len(csv_rows)) if variant[index] < 0)),
            tuple(sorted(_row_signature(json_rows[index]) for index in range(len(json_rows)) if index not in used_json)),
            tuple(
                sorted(
                    (_row_signature(csv_rows[index]), _row_signature(json_rows[match]))
                    for index, match in enumerate(variant)
                    if match >= 0
                )
            ),
        )
        if canonical in seen:
            continue
        seen.add(canonical)
        witnesses.append(
            {
                "canonical": canonical,
                "unmatched_csv": sorted(unmatched_csv),
                "unmatched_json": sorted(unmatched_json),
                "pairing": pairing,
            }
        )
    witnesses.sort(key=lambda item: item["canonical"])
    return witnesses


def _maximum_matchings(
    graph: list[list[int]], left_count: int
) -> list[list[int]]:
    """One maximum matching, plus alternatives only when cheap to enumerate.

    Enumeration exists solely to show that a reported minimum deletion set is
    one of several valid choices.  It is exponential in the worst case, so it
    runs only for small groups; larger groups report the single computed
    maximum matching.  The maximum *size* is always exact.
    """

    primary = _match_graph(graph)
    if left_count > _ENUMERATION_LIMIT:
        return [primary]
    size = sum(1 for value in primary if value >= 0)
    results: list[list[int]] = []

    def run(assignment: list[int], used_right: set[int], cursor: int) -> None:
        if len(results) >= _ENUMERATION_RESULTS:
            return
        matched = sum(1 for value in assignment if value >= 0)
        if matched + (left_count - cursor) < size:
            return
        if cursor == left_count:
            if matched == size:
                results.append(list(assignment))
            return
        for right in graph[cursor]:
            if right in used_right:
                continue
            assignment.append(right)
            used_right.add(right)
            run(assignment, used_right, cursor + 1)
            used_right.discard(right)
            assignment.pop()
        assignment.append(-1)
        run(assignment, used_right, cursor + 1)
        assignment.pop()

    run([], set(), 0)
    return results or [primary]


def _totals(
    rows: list[Observation], rule: PrecisionRule, declared: dict[str, int]
) -> dict[str, tuple[str, str]]:
    totals: dict[str, tuple[str, str]] = {}
    for metric in PM_FIELDS:
        with localcontext() as context:
            context.prec = _DECIMAL_CONTEXT
            low = Decimal(0)
            high = Decimal(0)
            for row in rows:
                row_low, row_high = _measurement_interval(
                    row.values[metric], _row_places(row, rule, metric, declared)
                )
                low += row_low
                high += row_high
        totals[metric] = (_decimal_str(low), _decimal_str(high))
    return totals


# ---------------------------------------------------------------------------
# Verdict assembly
# ---------------------------------------------------------------------------


def _precision_evidence(
    parsed_csv: ParsedObservations,
    parsed_json: ParsedObservations,
    rule: PrecisionRule,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {"rule": rule.as_dict(), "sources": {}}
    for name, parsed in (("csv", parsed_csv), ("jsonl", parsed_json)):
        entry: dict[str, Any] = {
            "declared": dict(parsed.declared),
            "declared_line": parsed.declared_line,
        }
        for metric in PM_FIELDS:
            origin, value = parsed.effective_places(rule, metric)
            entry[metric] = {"origin": origin, "places": value}
        evidence["sources"][name] = entry
    stable = {
        "rule": rule.as_dict(),
        "csv_declared": parsed_csv.declared,
        "json_declared": parsed_json.declared,
    }
    evidence["fingerprint"] = sha256_bytes(canonical_json(stable).encode("utf-8"))[:16]
    evidence["rule_text"] = (
        "value interval = [v - 0.5*10^-places, v + 0.5*10^-places]; "
        "places resolved per metric from the explicit rule, else the file "
        "declaration, else the last shown digit of each value"
    )
    return evidence


def _contract(
    name: str,
    status: str,
    notice: str,
    *,
    issues: list[Issue] | None = None,
    groups: list[GroupResult] | None = None,
    sources: list[ArtifactSource] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract": name,
        "status": status,
        "notice": notice,
        "sources": [source.evidence() for source in (sources or [])],
        "issues": [issue.as_dict() for issue in (issues or [])],
        "groups": [group.as_dict(name) for group in (groups or [])],
    }
    completed = groups or []
    if name == CONTRACT_AUDIT:
        if status == STATUS_PASS:
            payload["witness"] = {
                "kind": "global_row_bijection",
                "groups": [
                    {
                        "group": {"site_id": group.site_id, "observed_on": group.observed_on},
                        "pairs": len(group.pairing),
                        "pairing": [item.as_dict() for item in group.pairing],
                    }
                    for group in completed
                ],
            }
        elif status == STATUS_FAIL:
            payload["witness"] = {
                "kind": "minimal_removal",
                "unmatchable": [
                    {
                        "group": {"site_id": group.site_id, "observed_on": group.observed_on},
                        "witness_kind": group.witness_kind,
                        "witness_sources": group.witness_sources,
                        "unmatched_csv_lines": group.unmatched_csv,
                        "unmatched_json_lines": group.unmatched_json,
                        "partial_pairing": [item.as_dict() for item in group.pairing],
                    }
                    for group in completed
                    if group.audit == STATUS_FAIL
                ],
            }
        else:
            payload["witness"] = None
    else:
        if status == STATUS_PASS:
            payload["witness"] = {
                "kind": "aggregate_totals",
                "groups": [
                    {
                        "group": {"site_id": group.site_id, "observed_on": group.observed_on},
                        "count": len(group.csv_rows),
                        "csv_totals": {
                            "pm25": _pair(group.csv_total_pm25),
                            "pm10": _pair(group.csv_total_pm10),
                        },
                        "json_totals": {
                            "pm25": _pair(group.json_total_pm25),
                            "pm10": _pair(group.json_total_pm10),
                        },
                    }
                    for group in completed
                ],
            }
        elif status == STATUS_FAIL:
            payload["witness"] = {
                "kind": "aggregate_totals",
                "incompatible": [
                    {
                        "group": {"site_id": group.site_id, "observed_on": group.observed_on},
                        "reasons": list(group.reasons),
                        "csv_count": len(group.csv_rows),
                        "json_count": len(group.json_rows),
                        "csv_totals": {
                            "pm25": _pair(group.csv_total_pm25),
                            "pm10": _pair(group.csv_total_pm10),
                        },
                        "json_totals": {
                            "pm25": _pair(group.json_total_pm25),
                            "pm10": _pair(group.json_total_pm10),
                        },
                    }
                    for group in completed
                    if group.summary == STATUS_FAIL
                ],
            }
        else:
            payload["witness"] = None
    return payload


def _duplicates(
    csv_rows: list[Observation], json_rows: list[Observation]
) -> list[dict[str, Any]]:
    return _touch_duplicates(csv_rows, "csv") + _touch_duplicates(json_rows, "jsonl")


def _touch_duplicates(rows: list[Observation], source: str) -> list[dict[str, Any]]:
    """Report repeated identical physical observations without blocking pairing."""

    seen: dict[str, list[int]] = {}
    for row in rows:
        seen.setdefault(_row_signature(row), []).append(row.line)
    return [
        {"source": source, "signature": signature, "lines": lines}
        for signature, lines in sorted(seen.items())
        if len(lines) > 1
    ]


def _group(
    key: tuple[str, str],
    csv_rows: list[Observation],
    json_rows: list[Observation],
    rule: PrecisionRule,
    declared_csv: dict[str, int],
    declared_json: dict[str, int],
) -> GroupResult:
    # Canonicalise row order by *content* before any matching.  Physical file
    # order then cannot influence which rows survive, which minimum deletion
    # set is reported, or which of several valid pairings becomes the witness.
    csv_rows = sorted(csv_rows, key=lambda row: (_row_signature(row), row.line))
    json_rows = sorted(json_rows, key=lambda row: (_row_signature(row), row.line))
    result = GroupResult(
        site_id=key[0],
        observed_on=key[1],
        csv_rows=sorted(row.line for row in csv_rows),
        json_rows=sorted(row.line for row in json_rows),
        audit=STATUS_PASS,
        summary=STATUS_PASS,
    )
    csv_totals = _totals(csv_rows, rule, declared_csv)
    json_totals = _totals(json_rows, rule, declared_json)
    result.csv_total_pm25 = csv_totals["pm25"]
    result.csv_total_pm10 = csv_totals["pm10"]
    result.json_total_pm25 = json_totals["pm25"]
    result.json_total_pm10 = json_totals["pm10"]

    # Aggregate contract: counts plus exact total-interval compatibility only.
    if len(csv_rows) != len(json_rows):
        result.summary = STATUS_FAIL
        result.reasons.append("observation_count_mismatch")
    else:
        for metric in PM_FIELDS:
            if not _overlaps(
                (Decimal(csv_totals[metric][0]), Decimal(csv_totals[metric][1])),
                (Decimal(json_totals[metric][0]), Decimal(json_totals[metric][1])),
            ):
                result.summary = STATUS_FAIL
                result.reasons.append(f"total_{metric}_incompatible")

    # Audit contract: one global, whole-row bijection over physical rows.
    if not csv_rows or not json_rows:
        result.audit = STATUS_FAIL
        result.summary = STATUS_FAIL
        result.reasons.append("missing_counterpart_group")
        result.audit_reasons.append("missing_counterpart_group")
        result.witness_kind = "missing_counterpart_group"
        result.witness_sources = {
            "csv": [row.line for row in csv_rows],
            "jsonl": [row.line for row in json_rows],
        }
        result.unmatched_csv = [row.line for row in csv_rows]
        result.unmatched_json = [row.line for row in json_rows]
        result.duplicate_rows = _duplicates(csv_rows, json_rows)
        return result

    assignment = _augmenting_match(
        csv_rows, json_rows, rule, declared_csv, declared_json
    )
    matching_size = sum(1 for match in assignment if match >= 0)
    if len(csv_rows) == len(json_rows) == matching_size:
        result.pairing = _pairing_from_assignment(
            csv_rows, json_rows, assignment, rule, declared_csv, declared_json
        )
        result.duplicate_rows = _duplicates(csv_rows, json_rows)
        return result

    result.audit = STATUS_FAIL
    result.audit_reasons.append(
        "observation_count_mismatch"
        if len(csv_rows) != len(json_rows)
        else "no_global_row_bijection"
    )
    witnesses = _removal_witnesses(
        csv_rows, json_rows, rule, declared_csv, declared_json
    )
    chosen = witnesses[0]
    result.unmatched_csv = chosen["unmatched_csv"]
    result.unmatched_json = chosen["unmatched_json"]
    result.pairing = chosen["pairing"]
    result.witness_kind = "minimal_removal"
    result.witness_sources = {
        "csv": chosen["unmatched_csv"],
        "jsonl": chosen["unmatched_json"],
    }
    result.alternate_witnesses = [
        {
            "unmatched_csv_lines": item["unmatched_csv"],
            "unmatched_json_lines": item["unmatched_json"],
            "pairing": [pair.as_dict() for pair in item["pairing"]],
        }
        for item in witnesses[1:]
    ]
    result.minimum_removal_size = len(chosen["unmatched_csv"]) + len(
        chosen["unmatched_json"]
    )
    result.duplicate_rows = _duplicates(csv_rows, json_rows)
    return result


def _finalize(
    contracts: dict[str, dict[str, Any]],
    *,
    run_id: str | None,
    source_evidence: list[ArtifactSource],
    precision: dict[str, Any] | None,
) -> dict[str, Any]:
    # The digest identifies the *bytes under adjudication*, so it is
    # deliberately independent of the run, the contract verdict, and the
    # precision rule.  A rule change keeps the same source digest but produces
    # a new precision fingerprint and a freshly computed verdict.
    digest_basis = {
        "sources": [source.evidence() for source in source_evidence],
    }
    digest = sha256_bytes(canonical_json(digest_basis).encode("utf-8"))
    for payload in contracts.values():
        payload["source_digest"] = digest
        payload["precision"] = precision
    return contracts


def adjudicate(
    csv_source: ArtifactSource,
    json_source: ArtifactSource,
    *,
    precision: PrecisionRule = DEFAULT_PRECISION,
    run_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Compare one CSV and one JSONL source under both contracts.

    Returns ``{"observation_audit": ..., "daily_summary": ...}``.  Both are
    ``unknown`` when either artifact is missing, unaccepted, or no longer
    matches its accepted size and SHA-256.
    """

    sources = [csv_source, json_source]
    parsed_csv = ParsedObservations(kind="csv")
    parsed_json = ParsedObservations(kind="jsonl")
    # Parse each side independently so every defect is reported, not only the
    # first one encountered.  The issue list is always CSV-then-JSONL, so its
    # content and order never depend on which file was passed first.
    if csv_source.text is not None:
        parsed_csv = parse_csv(csv_source.text)
    if json_source.text is not None:
        parsed_json = parse_jsonl(json_source.text)
    blocking = (
        list(csv_source.issues)
        + list(json_source.issues)
        + list(parsed_csv.issues)
        + list(parsed_json.issues)
    )

    precision_evidence = _precision_evidence(parsed_csv, parsed_json, precision)
    if blocking:
        contracts = {
            CONTRACT_AUDIT: _contract(
                CONTRACT_AUDIT, STATUS_UNKNOWN, AUDIT_NOTICE, issues=blocking, sources=sources
            ),
            CONTRACT_SUMMARY: _contract(
                CONTRACT_SUMMARY, STATUS_UNKNOWN, SUMMARY_NOTICE, issues=blocking, sources=sources
            ),
        }
        return _finalize(
            contracts,
            run_id=run_id,
            source_evidence=sources,
            precision=precision_evidence,
        )

    grouped: dict[tuple[str, str], GroupView] = {}
    for row in parsed_csv.rows:
        grouped.setdefault(row.key, GroupView(key=row.key)).csv_rows.append(row)
    for row in parsed_json.rows:
        grouped.setdefault(row.key, GroupView(key=row.key)).json_rows.append(row)

    oversized = [
        Issue(
            "group",
            None,
            "group_too_large",
            {
                "site_id": key[0],
                "observed_on": key[1],
                "csv_rows": len(view.csv_rows),
                "json_rows": len(view.json_rows),
                "limit": MAX_GROUP_ROWS,
            },
        )
        for key, view in grouped.items()
        if len(view.csv_rows) > MAX_GROUP_ROWS or len(view.json_rows) > MAX_GROUP_ROWS
    ]
    if oversized:
        contracts = {
            CONTRACT_AUDIT: _contract(
                CONTRACT_AUDIT,
                STATUS_UNKNOWN,
                AUDIT_NOTICE,
                issues=oversized,
                sources=sources,
            ),
            CONTRACT_SUMMARY: _contract(
                CONTRACT_SUMMARY,
                STATUS_UNKNOWN,
                SUMMARY_NOTICE,
                issues=oversized,
                sources=sources,
            ),
        }
        return _finalize(
            contracts,
            run_id=run_id,
            source_evidence=sources,
            precision=precision_evidence,
        )

    groups: list[GroupResult] = []
    for key in sorted(grouped):
        view = grouped[key]
        if not view.csv_rows or not view.json_rows:
            missing = "jsonl" if view.csv_rows else "csv"
            present = view.csv_rows if view.csv_rows else view.json_rows
            group = GroupResult(
                site_id=key[0],
                observed_on=key[1],
                csv_rows=[row.line for row in view.csv_rows],
                json_rows=[row.line for row in view.json_rows],
                audit=STATUS_FAIL,
                summary=STATUS_FAIL,
            )
            group.reasons.append(f"missing_counterpart_group_in_{missing}")
            group.audit_reasons.append(f"missing_counterpart_group_in_{missing}")
            group.witness_kind = "missing_rows"
            group.witness_sources = {
                ("csv" if view.csv_rows else "jsonl"): [row.line for row in present]
            }
            group.duplicate_rows = _duplicates(view.csv_rows, view.json_rows)
            groups.append(group)
            continue
        groups.append(
            _group(
                key,
                view.csv_rows,
                view.json_rows,
                precision,
                parsed_csv.declared,
                parsed_json.declared,
            )
        )

    audit_status = (
        STATUS_PASS if all(group.audit == STATUS_PASS for group in groups) else STATUS_FAIL
    )
    summary_status = (
        STATUS_PASS if all(group.summary == STATUS_PASS for group in groups) else STATUS_FAIL
    )
    contracts = {
        CONTRACT_AUDIT: _contract(
            CONTRACT_AUDIT, audit_status, AUDIT_NOTICE, groups=groups, sources=sources
        ),
        CONTRACT_SUMMARY: _contract(
            CONTRACT_SUMMARY, summary_status, SUMMARY_NOTICE, groups=groups, sources=sources
        ),
    }
    return _finalize(
        contracts,
        run_id=run_id,
        source_evidence=sources,
        precision=precision_evidence,
    )
