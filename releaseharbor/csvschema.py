"""Declarative CSV schemas, bounded streaming parsing, and compatibility rulings.

This module deliberately never infers a column type from sample values. Every
type decision comes from the portal's declarative schema, so a ``text`` column
that happens to contain only digits is still treated as ``text``.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .errors import DiscoveryError
from .util import canonical_json, stable_id

COLUMN_TYPES = frozenset({"integer", "decimal", "text", "date"})
# The two independent consumer contracts.
POSITIONAL_CONTRACT = "positional"
NAME_BOUND_CONTRACT = "name_bound"
CONTRACTS = (POSITIONAL_CONTRACT, NAME_BOUND_CONTRACT)

# Bounds keep a hostile or malformed CSV from exhausting memory.
MAX_HEADER_BYTES = 65_536
MAX_RECORD_BYTES = 1_048_576
MAX_FIELD_BYTES = 262_144
MAX_RECORDS = 100_000
MAX_COLUMNS = 200
MAX_ERRORS = 20
_READ_CHUNK = 65_536

_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_SCHEMA_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9 ._\-]{0,127}\Z")


class CsvSchemaError(DiscoveryError):
    """A portal CSV schema declaration is structurally invalid."""


class CsvParseError(DiscoveryError):
    """Fatal streaming-parse failure (oversized record or unterminated quote)."""

    def __init__(self, code: str, line: int, column: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.line = line
        self.column = column


@dataclass(frozen=True)
class SchemaColumn:
    name: str
    type: str
    nullable: bool

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "nullable": self.nullable}

    @classmethod
    def from_dict(cls, raw: Any) -> "SchemaColumn":
        if not isinstance(raw, dict):
            raise CsvSchemaError("schema column must be an object")
        if set(raw) != {"name", "type", "nullable"}:
            raise CsvSchemaError("schema column has unknown or missing keys")
        name = raw["name"]
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise CsvSchemaError("schema column name is invalid")
        col_type = raw["type"]
        if col_type not in COLUMN_TYPES:
            raise CsvSchemaError(f"unsupported schema column type: {col_type!r}")
        nullable = raw["nullable"]
        if not isinstance(nullable, bool):
            raise CsvSchemaError("schema column nullable flag must be a boolean")
        return cls(name=name, type=col_type, nullable=nullable)


@dataclass(frozen=True)
class SchemaDeclaration:
    """The full declarative contract published by the portal for one CSV."""

    schema_version: str
    columns: tuple[SchemaColumn, ...]
    primary_key: tuple[str, ...]
    predecessor: str | None
    change_reason: str | None

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def column(self, name: str) -> SchemaColumn:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    def fingerprint(self) -> str:
        return stable_id(canonical_json(self.to_dict()))

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "columns": [column.to_dict() for column in self.columns],
            "primary_key": list(self.primary_key),
            "predecessor": self.predecessor,
            "change_reason": self.change_reason,
            "fingerprint": self.fingerprint(),
        }

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "columns": [column.to_dict() for column in self.columns],
            "primary_key": list(self.primary_key),
        }
        if self.predecessor is not None:
            data["predecessor"] = self.predecessor
        if self.change_reason is not None:
            data["change_reason"] = self.change_reason
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> "SchemaDeclaration":
        if not isinstance(raw, dict):
            raise CsvSchemaError("CSV schema must be an object")
        allowed = {"schema_version", "columns", "primary_key", "predecessor", "change_reason"}
        unknown = set(raw) - allowed
        if unknown:
            raise CsvSchemaError(f"CSV schema has unknown keys: {', '.join(sorted(unknown))}")
        required = {"schema_version", "columns", "primary_key"}
        missing = required - set(raw)
        if missing:
            raise CsvSchemaError(
                f"CSV schema missing keys: {', '.join(sorted(missing))}"
            )
        version = raw["schema_version"]
        if not isinstance(version, str) or not _SCHEMA_VERSION.fullmatch(version):
            raise CsvSchemaError("schema_version is invalid")
        columns_raw = raw["columns"]
        if not isinstance(columns_raw, list) or not 1 <= len(columns_raw) <= MAX_COLUMNS:
            raise CsvSchemaError("schema columns must contain 1..%d entries" % MAX_COLUMNS)
        columns = tuple(SchemaColumn.from_dict(item) for item in columns_raw)
        names = [column.name for column in columns]
        if len(set(names)) != len(names):
            raise CsvSchemaError("schema contains duplicate column names")
        primary_key_raw = raw["primary_key"]
        if not isinstance(primary_key_raw, list) or not primary_key_raw:
            raise CsvSchemaError("schema primary_key must be a non-empty list")
        if len(primary_key_raw) > len(columns):
            raise CsvSchemaError("schema primary_key lists more columns than exist")
        primary_key: list[str] = []
        for name in primary_key_raw:
            if not isinstance(name, str) or name not in names:
                raise CsvSchemaError("schema primary_key references an unknown column")
            if name in primary_key:
                raise CsvSchemaError("schema primary_key contains a duplicate column")
            pk_column = next(column for column in columns if column.name == name)
            if pk_column.nullable:
                raise CsvSchemaError("schema primary_key columns must be non-nullable")
            primary_key.append(name)
        predecessor = raw.get("predecessor")
        if predecessor is not None and (
            not isinstance(predecessor, str) or not _SCHEMA_VERSION.fullmatch(predecessor)
        ):
            raise CsvSchemaError("schema predecessor is invalid")
        change_reason = raw.get("change_reason")
        if change_reason is not None and (
            not isinstance(change_reason, str)
            or not change_reason.strip()
            or len(change_reason) > 1000
        ):
            raise CsvSchemaError("schema change_reason must be a non-empty string")
        return cls(
            schema_version=version,
            columns=columns,
            primary_key=tuple(primary_key),
            predecessor=predecessor,
            change_reason=change_reason.strip() if isinstance(change_reason, str) else None,
        )


@dataclass
class CsvValidationReport:
    """Deterministic result of validating accepted CSV bytes against a schema."""

    ok: bool
    header: tuple[str, ...]
    record_count: int
    errors: list[dict[str, Any]] = field(default_factory=list)
    fatal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "header": list(self.header),
            "record_count": self.record_count,
            "errors": self.errors,
            "fatal": self.fatal,
        }


class _StreamingCsvParser:
    """Bounded, quote-aware, streaming RFC-4180-style CSV record reader.

    Yields ``(line_number, record)`` tuples. Records spanning multiple physical
    lines (a quoted field containing a newline) are joined transparently and
    reported under the line where the record starts. Bounds are enforced on
    decoded field/record sizes so a pathological file cannot grow memory without
    limit.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._buffer = ""
        self._line_number = 0
        self._record_start_line = 1
        self._at_eof = False
        self._decoder = codecs.getincrementaldecoder("utf-8")()

    def _fill(self, handle) -> bool:
        chunk = handle.read(_READ_CHUNK)
        if not chunk:
            self._at_eof = True
            try:
                tail = self._decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise CsvParseError(
                    "invalid_encoding",
                    self._record_start_line,
                    0,
                    f"file is not valid UTF-8: {exc.reason}",
                ) from exc
            if tail:
                self._buffer += tail
            return False
        try:
            self._buffer += self._decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise CsvParseError(
                "invalid_encoding",
                self._record_start_line,
                0,
                f"file is not valid UTF-8: {exc.reason}",
            ) from exc
        return True

    def __iter__(self):
        with self.path.open("rb") as handle:
            field_chars: list[str] = []
            record: list[str] = []
            in_quotes = False
            record_size = 0
            field_size = 0
            saw_content = False
            while True:
                if not self._buffer:
                    if self._at_eof or not self._fill(handle):
                        break
                char = self._buffer[0]
                self._buffer = self._buffer[1:]
                if char == "\n":
                    self._line_number += 1
                if in_quotes:
                    if char == '"':
                        # Lookahead for an escaped quote; buffer a chunk if needed.
                        if not self._buffer and not self._at_eof:
                            self._fill(handle)
                        if self._buffer[:1] == '"':
                            self._buffer = self._buffer[1:]
                            field_chars.append('"')
                            field_size += 1
                            saw_content = True
                            self._guard_field(field_size)
                            continue
                        in_quotes = False
                        continue
                    field_chars.append(char)
                    field_size += 1
                    saw_content = True
                    record_size += 1
                    self._guard_field(field_size)
                    self._guard_record(record_size)
                    continue
                if char == '"':
                    if field_chars:
                        raise CsvParseError(
                            "unescaped_quote",
                            self._record_start_line,
                            len(record) + 1,
                            "quote appears inside an unquoted field",
                        )
                    in_quotes = True
                    saw_content = True
                    continue
                if char == ",":
                    record.append("".join(field_chars))
                    field_chars = []
                    field_size = 0
                    record_size += 1
                    self._guard_record(record_size)
                    continue
                if char in "\r\n":
                    if char == "\r":
                        # Swallow the LF of a CRLF pair.
                        if not self._buffer and not self._at_eof:
                            self._fill(handle)
                        if self._buffer[:1] == "\n":
                            self._buffer = self._buffer[1:]
                            self._line_number += 1
                    if not saw_content:
                        # A bare blank line (typically the final newline) is not
                        # a data record; quoted empty fields still set the flag.
                        record = []
                        field_chars = []
                        field_size = 0
                        record_size = 0
                        self._record_start_line = self._line_number + 1
                        continue
                    record.append("".join(field_chars))
                    completed = record
                    completed_line = self._record_start_line
                    record = []
                    field_chars = []
                    field_size = 0
                    record_size = 0
                    saw_content = False
                    self._record_start_line = self._line_number + 1
                    yield completed_line, completed
                    continue
                field_chars.append(char)
                field_size += 1
                saw_content = True
                record_size += 1
                self._guard_field(field_size)
                self._guard_record(record_size)
            if in_quotes:
                raise CsvParseError(
                    "unterminated_quote",
                    self._record_start_line,
                    len(record) + 1,
                    "quoted field is not closed before end of file",
                )
            if saw_content:
                record.append("".join(field_chars))
                yield self._record_start_line, record

    def _guard_field(self, size: int) -> None:
        if size > MAX_FIELD_BYTES:
            raise CsvParseError(
                "field_too_large",
                self._record_start_line,
                0,
                f"field exceeds {MAX_FIELD_BYTES} bytes",
            )

    def _guard_record(self, size: int) -> None:
        if size > MAX_RECORD_BYTES:
            raise CsvParseError(
                "record_too_large",
                self._record_start_line,
                0,
                f"record exceeds {MAX_RECORD_BYTES} bytes",
            )


def _add_error(errors: list[dict[str, Any]], error: dict[str, Any]) -> bool:
    """Append the first deterministic errors; return False when the cap is hit."""
    if len(errors) >= MAX_ERRORS:
        return False
    errors.append(error)
    return True


def _validate_value(column: SchemaColumn, value: str) -> str | None:
    """Return None when the value satisfies the declared type, else a reason."""
    if value == "":
        if column.nullable:
            return None
        return "null value in non-nullable column"
    if column.type == "integer":
        if not _INTEGER.fullmatch(value):
            return f"value {value!r} is not a valid integer"
        return None
    if column.type == "decimal":
        if not _DECIMAL.fullmatch(value):
            return f"value {value!r} is not a valid decimal"
        return None
    if column.type == "date":
        if not _DATE.fullmatch(value):
            return f"value {value!r} is not an ISO date"
        try:
            date.fromisoformat(value)
        except ValueError:
            return f"value {value!r} is not a valid calendar date"
        return None
    # text accepts any non-empty scalar already read as a single field.
    return None


def validate_csv(path: Path, declaration: SchemaDeclaration) -> CsvValidationReport:
    """Stream an accepted CSV file and rule on schema conformance.

    Byte-level acceptance (size + SHA-256) happens before this function runs;
    a failure here never upgrades the artifact to schema-compatible.
    """
    errors: list[dict[str, Any]] = []
    header: tuple[str, ...] = ()
    record_count = 0
    expected_width = len(declaration.columns)
    pk_indexes = [declaration.column_names.index(name) for name in declaration.primary_key]
    seen_primary_keys: set[tuple[str, ...]] = set()
    try:
        if not path.is_file():
            raise CsvParseError("missing_file", 0, 0, f"CSV file is missing: {path}")
        if path.stat().st_size > MAX_RECORD_BYTES * MAX_RECORDS:
            raise CsvParseError("file_too_large", 0, 0, "CSV exceeds the bounded parse limit")
        parser = _StreamingCsvParser(path)
        for line_number, record in parser:
            if not header:
                if sum(len(part.encode("utf-8")) for part in record) > MAX_HEADER_BYTES:
                    raise CsvParseError(
                        "header_too_large", line_number, 0, "CSV header exceeds byte budget"
                    )
                header = tuple(record)
                duplicates = sorted(
                    {name for name in header if header.count(name) > 1}
                )
                if duplicates:
                    _add_error(
                        errors,
                        {
                            "code": "duplicate_header",
                            "line": line_number,
                            "column": 0,
                            "reason": "duplicate header column(s): "
                            + ", ".join(duplicates),
                        },
                    )
                if list(header) != list(declaration.column_names):
                    _add_error(
                        errors,
                        {
                            "code": "header_mismatch",
                            "line": line_number,
                            "column": 0,
                            "reason": "CSV header does not match the declared schema",
                            "observed": list(header),
                            "declared": list(declaration.column_names),
                        },
                    )
                if len(record) != expected_width:
                    _add_error(
                        errors,
                        {
                            "code": "column_count",
                            "line": line_number,
                            "column": 0,
                            "reason": f"header has {len(record)} columns, schema declares "
                            f"{expected_width}",
                        },
                    )
                continue
            record_count += 1
            if record_count > MAX_RECORDS:
                raise CsvParseError(
                    "too_many_records", line_number, 0, f"CSV exceeds {MAX_RECORDS} records"
                )
            if len(record) != expected_width:
                _add_error(
                    errors,
                    {
                        "code": "column_count",
                        "line": line_number,
                        "column": 0,
                        "reason": f"record has {len(record)} fields, expected {expected_width}",
                    },
                )
                continue
            for index, (column, value) in enumerate(
                zip(declaration.columns, record), start=1
            ):
                reason = _validate_value(column, value)
                if reason is not None:
                    _add_error(
                        errors,
                        {
                            "code": "invalid_value",
                            "line": line_number,
                            "column": index,
                            "column_name": column.name,
                            "reason": reason,
                        },
                    )
            key_values = tuple(record[index] for index in pk_indexes)
            if any(value == "" for value in key_values):
                _add_error(
                    errors,
                    {
                        "code": "primary_key_null",
                        "line": line_number,
                        "column": 0,
                        "reason": "primary key column is empty",
                    },
                )
            elif key_values in seen_primary_keys:
                _add_error(
                    errors,
                    {
                        "code": "primary_key_duplicate",
                        "line": line_number,
                        "column": 0,
                        "reason": "duplicate primary key within file",
                        "primary_key": list(key_values),
                    },
                )
            else:
                seen_primary_keys.add(key_values)
    except CsvParseError as exc:
        return CsvValidationReport(
            ok=False,
            header=header,
            record_count=record_count,
            errors=errors,
            fatal={"code": exc.code, "line": exc.line, "column": exc.column, "reason": str(exc)},
        )
    if not header:
        _add_error(
            errors,
            {"code": "empty_file", "line": 0, "column": 0, "reason": "CSV contains no header"},
        )
    return CsvValidationReport(
        ok=not errors,
        header=header,
        record_count=record_count,
        errors=errors,
        fatal=None,
    )


@dataclass(frozen=True)
class ContractVerdict:
    contract: str
    compatible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "compatible": self.compatible,
            "reasons": list(self.reasons),
        }


def _positional_verdict(
    previous: SchemaDeclaration, current: SchemaDeclaration
) -> ContractVerdict:
    """Position-bound consumer: existing columns keep index, name, type, nullability."""
    reasons: list[str] = []
    old_columns = previous.columns
    new_columns = current.columns
    if len(new_columns) < len(old_columns):
        reasons.append(
            f"column count reduced from {len(old_columns)} to {len(new_columns)}; "
            "existing columns were removed or reordered"
        )
    for index, old_column in enumerate(old_columns):
        if index >= len(new_columns):
            reasons.append(f"column {index + 1} ({old_column.name}) was removed")
            continue
        new_column = new_columns[index]
        if new_column.name != old_column.name:
            reasons.append(
                f"column {index + 1} name changed from {old_column.name!r} to "
                f"{new_column.name!r}"
            )
        if new_column.type != old_column.type:
            reasons.append(
                f"column {index + 1} ({old_column.name}) type changed from "
                f"{old_column.type} to {new_column.type}"
            )
        if new_column.nullable != old_column.nullable:
            reasons.append(
                f"column {index + 1} ({old_column.name}) nullability changed from "
                f"{old_column.nullable} to {new_column.nullable}"
            )
    # New columns may only be appended; anything inserted before an existing
    # column already surfaces above as a name/type shift at that index.
    if current.primary_key != previous.primary_key:
        reasons.append("primary key semantics changed")
    return ContractVerdict(POSITIONAL_CONTRACT, not reasons, tuple(reasons))


def _name_bound_verdict(
    previous: SchemaDeclaration, current: SchemaDeclaration
) -> ContractVerdict:
    """Name-bound consumer: reordering and added nullable columns are acceptable."""
    reasons: list[str] = []
    old_by_name = {column.name: column for column in previous.columns}
    new_by_name = {column.name: column for column in current.columns}
    for name, old_column in old_by_name.items():
        if name not in new_by_name:
            reasons.append(f"existing column {name!r} was removed")
            continue
        new_column = new_by_name[name]
        if new_column.type != old_column.type:
            # A widening such as decimal -> text is still a breaking type change:
            # sample values are never inspected to certify it as safe.
            reasons.append(
                f"column {name!r} type changed from {old_column.type} to "
                f"{new_column.type}; type relaxation is not accepted"
            )
        if old_column.nullable is False and new_column.nullable is True:
            reasons.append(
                f"column {name!r} weakened its non-null promise to nullable"
            )
    for name, new_column in new_by_name.items():
        if name not in old_by_name and not new_column.nullable:
            reasons.append(f"new column {name!r} must be nullable for existing consumers")
    if current.primary_key != previous.primary_key:
        reasons.append("primary key semantics changed")
    return ContractVerdict(NAME_BOUND_CONTRACT, not reasons, tuple(reasons))


def adjudicate_compatibility(
    previous: SchemaDeclaration | None, current: SchemaDeclaration
) -> list[ContractVerdict]:
    """Rule for both consumer contracts based only on the declarations."""
    if previous is None:
        return [
            ContractVerdict(
                contract,
                True,
                ("first schema version in this lineage; no predecessor to compare",),
            )
            for contract in CONTRACTS
        ]
    return [
        _positional_verdict(previous, current),
        _name_bound_verdict(previous, current),
    ]


def schema_lineage_key(dataset_id: str, filename: str) -> str:
    """Lineages are identified by dataset + published filename."""
    return stable_id(dataset_id, filename)
