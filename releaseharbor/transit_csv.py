"""Located CSV parsing for transit counts, labels, and allowed edges."""

from __future__ import annotations

import csv
import io
import re

from .transit_model import (
    Diagnostic,
    Edge,
    Label,
    Station,
)

STATION_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
ZONE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}\Z")
MAX_ROWS = 5000
FREE_ALIASES = {
    "source": "SOURCE",
    "@source": "SOURCE",
    "sink": "SINK",
    "@sink": "SINK",
    "exit": "SINK",
    "@exit": "SINK",
}
EDGE_COLUMNS = ("from", "to", "min", "max")


def _decode(data: bytes, role: str, filename: str) -> tuple[list[list[str]] | None, list[Diagnostic]]:
    problems: list[Diagnostic] = []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [Diagnostic("invalid_encoding", role, filename, None, "CSV is not UTF-8")]
    if "\x00" in text:
        return None, [Diagnostic("invalid_encoding", role, filename, None, "CSV contains NUL")]
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        return None, [Diagnostic("invalid_csv", role, filename, None, str(exc))]
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        problems.append(Diagnostic("empty_file", role, filename, None, "CSV has no rows"))
        return None, problems
    if len(rows) > MAX_ROWS + 1:
        problems.append(
            Diagnostic("too_many_rows", role, filename, None, f"more than {MAX_ROWS} data rows")
        )
        return None, problems
    return rows, problems


def _header(rows, role, filename, required) -> tuple[dict[str, int] | None, list[Diagnostic]]:
    header = [cell.strip() for cell in rows[0]]
    problems: list[Diagnostic] = []
    if len(set(header)) != len(header):
        problems.append(
            Diagnostic("duplicate_column", role, filename, 1, "header repeats a column name")
        )
        return None, problems
    missing = [name for name in required if name not in header]
    if missing:
        problems.append(
            Diagnostic(
                "missing_column",
                role,
                filename,
                1,
                "missing column(s): " + ", ".join(missing),
            )
        )
        return None, problems
    return {name: header.index(name) for name in required}, problems


def _integer(text: str) -> int | None:
    text = text.strip()
    if not re.fullmatch(r"[+-]?\d{1,12}", text):
        return None
    return int(text)


def parse_stations(data: bytes, role: str, filename: str) -> tuple[dict[str, Station], list[Diagnostic]]:
    rows, problems = _decode(data, role, filename)
    if rows is None:
        return {}, problems
    columns, more = _header(rows, role, filename, ("station_code", "weekday_boardings"))
    problems.extend(more)
    if columns is None:
        return {}, problems
    stations: dict[str, Station] = {}
    seen_line: dict[str, int] = {}
    for offset, row in enumerate(rows[1:], start=2):
        if len(row) != len(rows[0]):
            problems.append(
                Diagnostic("row_width", role, filename, offset, "row has the wrong column count")
            )
            continue
        code = row[columns["station_code"]].strip()
        if not STATION_CODE.fullmatch(code):
            problems.append(
                Diagnostic("invalid_station_code", role, filename, offset, f"bad code {code!r}")
            )
            continue
        amount = _integer(row[columns["weekday_boardings"]])
        if amount is None or amount < 0:
            problems.append(
                Diagnostic(
                    "invalid_count",
                    role,
                    filename,
                    offset,
                    f"station {code} has a non-integer or negative count",
                )
            )
            continue
        if code in stations:
            problems.append(
                Diagnostic(
                    "duplicate_station",
                    role,
                    filename,
                    offset,
                    f"station {code} repeats line {seen_line[code]}",
                )
            )
            continue
        stations[code] = Station(code, amount, offset)
        seen_line[code] = offset
    if not stations and not problems:
        problems.append(Diagnostic("empty_file", role, filename, None, "no station rows"))
    return stations, problems


def parse_labels(data: bytes, role: str, filename: str) -> tuple[dict[str, Label], list[Diagnostic]]:
    rows, problems = _decode(data, role, filename)
    if rows is None:
        return {}, problems
    columns, more = _header(rows, role, filename, ("station_code", "zone"))
    problems.extend(more)
    if columns is None:
        return {}, problems
    labels: dict[str, Label] = {}
    seen_line: dict[str, int] = {}
    for offset, row in enumerate(rows[1:], start=2):
        if len(row) != len(rows[0]):
            problems.append(
                Diagnostic("row_width", role, filename, offset, "row has the wrong column count")
            )
            continue
        code = row[columns["station_code"]].strip()
        if not STATION_CODE.fullmatch(code):
            problems.append(
                Diagnostic("invalid_station_code", role, filename, offset, f"bad code {code!r}")
            )
            continue
        zone = row[columns["zone"]].strip()
        if not ZONE.fullmatch(zone):
            problems.append(
                Diagnostic("invalid_zone", role, filename, offset, f"bad zone {zone!r}")
            )
            continue
        if code in labels:
            problems.append(
                Diagnostic(
                    "duplicate_label",
                    role,
                    filename,
                    offset,
                    f"label for {code} repeats line {seen_line[code]}",
                )
            )
            continue
        labels[code] = Label(code, zone, offset)
        seen_line[code] = offset
    if not labels and not problems:
        problems.append(Diagnostic("empty_file", role, filename, None, "no label rows"))
    return labels, problems


def parse_edges(data: bytes, role: str, version_id: str, filename: str) -> tuple[list[Edge], list[Diagnostic]]:
    rows, problems = _decode(data, role, filename)
    if rows is None:
        return [], problems
    columns, more = _header(rows, role, filename, EDGE_COLUMNS)
    problems.extend(more)
    if columns is None:
        return [], problems
    edges: list[Edge] = []
    seen: dict[tuple[str, str], int] = {}
    for offset, row in enumerate(rows[1:], start=2):
        if len(row) != len(rows[0]):
            problems.append(
                Diagnostic("row_width", role, filename, offset, "row has the wrong column count")
            )
            continue
        source = row[columns["from"]].strip()
        target = row[columns["to"]].strip()
        source = FREE_ALIASES.get(source.lower(), source)
        target = FREE_ALIASES.get(target.lower(), target)
        for value, label in ((source, "from"), (target, "to")):
            if value in {"SOURCE", "SINK"}:
                continue
            if not STATION_CODE.fullmatch(value):
                problems.append(
                    Diagnostic("invalid_station_code", role, filename, offset, f"bad {label} {value!r}")
                )
                break
        else:
            minimum = _integer(row[columns["min"]])
            maximum = _integer(row[columns["max"]])
            if minimum is None or maximum is None:
                problems.append(
                    Diagnostic("invalid_capacity", role, filename, offset, "min/max must be integers")
                )
                continue
            if minimum < 0 or maximum < 0:
                problems.append(
                    Diagnostic("negative_capacity", role, filename, offset, "min/max are negative")
                )
                continue
            if minimum > maximum:
                problems.append(
                    Diagnostic(
                        "inverted_capacity",
                        role,
                        filename,
                        offset,
                        f"min {minimum} exceeds max {maximum}",
                    )
                )
                continue
            if source == "SOURCE" and target == "SOURCE" or source == "SINK" and target == "SINK":
                problems.append(
                    Diagnostic("reserved_edge", role, filename, offset, "free node used as both ends")
                )
                continue
            key = (source, target)
            if key in seen:
                problems.append(
                    Diagnostic(
                        "duplicate_edge",
                        role,
                        filename,
                        offset,
                        f"edge {source}->{target} repeats line {seen[key]}",
                    )
                )
                continue
            seen[key] = offset
            edges.append(Edge(source, target, minimum, maximum, offset, len(edges) + 1))
    if not edges and not problems:
        problems.append(Diagnostic("empty_file", role, filename, None, "no edge rows"))
    return edges, problems
