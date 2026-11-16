"""Discovery result model types (kept import-compatible via discovery.py)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .coverage import CoverageDeclaration, PageEvidence
from .util import stable_id


@dataclass(frozen=True)
class ArtifactRef:
    dataset_id: str
    dataset_title: str
    version_id: str
    version_label: str
    channel: str
    released_at: str
    filename: str
    url: str
    media_type: str
    expected_size: int
    expected_sha256: str

    @property
    def artifact_uid(self) -> str:
        return stable_id(self.dataset_id, self.version_id, self.filename)


@dataclass
class DiscoveredVersion:
    version_id: str
    label: str
    channel: str
    released_at: str
    source_url: str
    artifacts: list[ArtifactRef] = field(default_factory=list)


@dataclass
class DiscoveredDataset:
    dataset_id: str
    title: str
    publisher: str
    license: str
    source_url: str
    versions: list[DiscoveredVersion] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    datasets: list[DiscoveredDataset]
    pages_fetched: int
    blocked: list[dict]
    issues: list[dict]
    declarations: list[CoverageDeclaration] = field(default_factory=list)
    evidence: list[PageEvidence] = field(default_factory=list)
