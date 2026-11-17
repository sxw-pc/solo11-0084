"""Data types shared by transit CSV parsing, flow solving, and reporting.

Every rejected input yields a located :class:`Diagnostic` (role, filename,
line, code) so callers can refuse to publish a bound when any part of the
evidence is ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OLD_ROLE = "base"
NEW_ROLE = "next"

@dataclass
class Diagnostic:
    code: str
    role: str
    filename: str
    line: int | None
    detail: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "role": self.role,
            "filename": self.filename,
            "line": self.line,
            "detail": self.detail,
        }


@dataclass
class ArtifactEvidence:
    role: str
    dataset_id: str
    version_id: str
    filename: str
    local_path: str | None
    expected_size: int
    expected_sha256: str
    status: str

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "dataset": self.dataset_id,
            "version": self.version_id,
            "filename": self.filename,
            "local_path": self.local_path,
            "expected_size": self.expected_size,
            "expected_sha256": self.expected_sha256,
            "status": self.status,
        }


@dataclass
class Station:
    code: str
    amount: int
    line: int


@dataclass
class Label:
    code: str
    zone: str
    line: int


@dataclass
class Edge:
    source: str
    target: str
    minimum: int
    maximum: int
    line: int
    index: int

    @property
    def key(self) -> str:
        return f"e{self.index:02d}:{self.source}->{self.target}"


@dataclass
class SiteSide:
    role: str
    version_id: str
    file_sha256: str
    file_size: int
    local_path: str
    label_sha256: str = ""
    label_size: int = 0
    stations: dict[str, Station] = field(default_factory=dict)
    labels: dict[str, Label] = field(default_factory=dict)

    def zone_groups(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for code in sorted(self.stations):
            label = self.labels.get(code)
            if label is None:
                continue
            groups.setdefault(label.zone, []).append(code)
        return groups

    def amount(self, codes) -> int:
        return sum(self.stations[code].amount for code in codes)


@dataclass
class EdgeTable:
    version_id: str
    file_sha256: str
    file_size: int
    local_path: str
    edges: list[Edge] = field(default_factory=list)


@dataclass
class TransitInputs:
    dataset_id: str
    old: SiteSide | None = None
    new: SiteSide | None = None
    edge_tables: list[EdgeTable] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    old_evidence: dict = field(default_factory=dict)
    new_evidence: dict = field(default_factory=dict)
    old_label_evidence: dict = field(default_factory=dict)
    new_label_evidence: dict = field(default_factory=dict)
    edge_evidence: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.diagnostics and self.old is not None and self.new is not None

    def evidence(self) -> list[dict]:
        return [
            item
            for item in (
                self.old_evidence,
                self.new_evidence,
                self.old_label_evidence,
                self.new_label_evidence,
            )
            if item
        ]
