"""Bounded discovery of datasets, versions, and release artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urldefrag, urljoin

from .artifactmeta import parse_artifact_attrs
from .errors import DiscoveryError, SecurityBoundaryError, TransportError
from .htmlparse import has_class, parse_portal_html
from .httpclient import SafeHttpClient
from .security import ValidatedTarget
from .util import safe_segment, stable_id

EventSink = Callable[[str, str, dict], None]


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
    chunk_size: int | None = None
    chunk_sha256: tuple[str, ...] = ()

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


class _BudgetExceeded(DiscoveryError):
    pass


class Discovery:
    """Traverses only the fixture portal's documented link classes."""

    def __init__(self, client: SafeHttpClient, config, on_event: EventSink | None = None) -> None:
        self.client = client
        self.config = config
        self.on_event = on_event or (lambda code, subject, detail: None)
        self.pages_fetched = 0
        self.blocked: list[dict] = []
        self.issues: list[dict] = []

    def _emit(self, code: str, subject: str, detail: dict) -> None:
        self.on_event(code, subject, detail)

    def _issue(self, code: str, subject: str, detail: dict) -> None:
        item = {"code": code, "subject": subject, **detail}
        self.issues.append(item)
        self._emit(code, subject, detail)

    def _load(self, url: str) -> str:
        if self.pages_fetched >= self.config.discovery.max_pages:
            self._emit(
                "discovery.page_budget",
                "page-budget",
                {"max_pages": self.config.discovery.max_pages},
            )
            raise _BudgetExceeded("portal page budget exhausted")
        with self.client.open("GET", url) as response:
            self.pages_fetched += 1
            if response.status != 200:
                self._issue(
                    "page.failed", url, {"status": response.status, "reason": "unexpected status"}
                )
                raise DiscoveryError(f"unexpected status {response.status} for {url}")
            text = response.text(1_000_000)
        self._emit("page.fetched", url, {"status": 200, "bytes": len(text.encode("utf-8"))})
        return text

    def _link(self, base_url: str, href: str) -> ValidatedTarget | None:
        if not isinstance(href, str) or not href.strip():
            return None
        candidate, _fragment = urldefrag(urljoin(base_url, href.strip()))
        try:
            return self.client.validate(candidate)
        except SecurityBoundaryError as exc:
            self.blocked.append({"url": candidate, "reason": str(exc)})
            return None

    def discover(self) -> DiscoveryResult:
        home = self.client.validate(self.config.portal.base_url).url
        page = parse_portal_html(self._load(home))
        links: dict[str, ValidatedTarget] = {}
        for anchor in page.anchors:
            if not has_class(anchor.attrs, "dataset-link"):
                continue
            target = self._link(home, anchor.attrs.get("href", ""))
            if target is not None:
                links[target.url] = target
        ordered = [links[key] for key in sorted(links)]
        if len(ordered) > self.config.discovery.max_datasets:
            self._emit(
                "discovery.dataset_limit",
                "datasets",
                {"found": len(ordered), "kept": self.config.discovery.max_datasets},
            )
            ordered = ordered[: self.config.discovery.max_datasets]
        datasets: list[DiscoveredDataset] = []
        seen_ids: set[str] = set()
        for target in ordered:
            try:
                dataset = self._discover_dataset(target.url)
            except (DiscoveryError, TransportError, SecurityBoundaryError) as exc:
                self._issue("dataset.failed", target.url, {"reason": str(exc)})
                continue
            if dataset.dataset_id in seen_ids:
                continue
            seen_ids.add(dataset.dataset_id)
            datasets.append(dataset)
        datasets.sort(key=lambda item: item.dataset_id)
        return DiscoveryResult(datasets, self.pages_fetched, self.blocked, self.issues)

    def _discover_dataset(self, page_url: str) -> DiscoveredDataset:
        parsed = parse_portal_html(self._load(page_url))
        main = parsed.main_attrs
        if main.get("data-page") != "dataset":
            raise DiscoveryError("dataset page marker is missing")
        dataset_id = safe_segment(main.get("data-dataset-id", ""), "dataset id")
        title = main.get("data-title", "").strip()
        publisher = main.get("data-publisher", "").strip()
        license_name = main.get("data-license", "").strip()
        if not title or not publisher or not license_name:
            raise DiscoveryError("dataset metadata is incomplete")
        dataset = DiscoveredDataset(
            dataset_id=dataset_id,
            title=title,
            publisher=publisher,
            license=license_name,
            source_url=page_url,
        )
        version_links: dict[str, ValidatedTarget] = {}
        for anchor in parsed.anchors:
            if not has_class(anchor.attrs, "version-link"):
                continue
            target = self._link(page_url, anchor.attrs.get("href", ""))
            if target is not None:
                version_links[target.url] = target
        ordered = [version_links[key] for key in sorted(version_links)]
        if len(ordered) > self.config.discovery.max_versions:
            self._emit(
                "discovery.version_limit",
                dataset.dataset_id,
                {"found": len(ordered), "kept": self.config.discovery.max_versions},
            )
            ordered = ordered[: self.config.discovery.max_versions]
        seen_versions: set[str] = set()
        for target in ordered:
            try:
                version = self._discover_version(dataset, target.url)
            except (DiscoveryError, TransportError, SecurityBoundaryError) as exc:
                self._issue("version.failed", target.url, {"reason": str(exc)})
                continue
            if version.channel not in self.config.discovery.channels:
                self._emit(
                    "discovery.channel_filtered",
                    version.version_id,
                    {"channel": version.channel},
                )
                continue
            if version.version_id in seen_versions:
                continue
            seen_versions.add(version.version_id)
            dataset.versions.append(version)
        dataset.versions.sort(key=lambda item: item.version_id)
        return dataset

    def _discover_version(
        self, dataset: DiscoveredDataset, page_url: str
    ) -> DiscoveredVersion:
        parsed = parse_portal_html(self._load(page_url))
        main = parsed.main_attrs
        if main.get("data-page") != "version":
            raise DiscoveryError("version page marker is missing")
        if main.get("data-dataset-id") != dataset.dataset_id:
            raise DiscoveryError("version page belongs to another dataset")
        version_id = safe_segment(main.get("data-version-id", ""), "version id")
        label = main.get("data-version-label", "").strip()
        channel = main.get("data-channel", "").strip()
        released = main.get("data-released-at", "").strip()
        if not label or not channel or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", released):
            raise DiscoveryError("version metadata is incomplete")
        version = DiscoveredVersion(version_id, label, channel, released, page_url)
        artifact_links: dict[str, ValidatedTarget] = {}
        for anchor in parsed.anchors:
            if not has_class(anchor.attrs, "artifact-link"):
                continue
            target = self._link(page_url, anchor.attrs.get("href", ""))
            if target is not None:
                artifact_links[target.url] = target
        ordered = [artifact_links[key] for key in sorted(artifact_links)]
        if len(ordered) > self.config.discovery.max_artifacts:
            self._emit(
                "discovery.artifact_limit",
                version_id,
                {"found": len(ordered), "kept": self.config.discovery.max_artifacts},
            )
            ordered = ordered[: self.config.discovery.max_artifacts]
        seen_names: set[str] = set()
        for target in ordered:
            attributes = parse_artifact_attrs(parsed, target.url, page_url, self._issue)
            if attributes is None:
                continue
            filename = attributes["filename"]
            if filename in seen_names:
                continue
            seen_names.add(filename)
            version.artifacts.append(
                ArtifactRef(
                    dataset_id=dataset.dataset_id,
                    dataset_title=dataset.title,
                    version_id=version_id,
                    version_label=label,
                    channel=channel,
                    released_at=released,
                    filename=filename,
                    url=target.url,
                    media_type=attributes["media_type"],
                    expected_size=attributes["size"],
                    expected_sha256=attributes["sha256"],
                    chunk_size=attributes["chunk_size"],
                    chunk_sha256=attributes["chunk_sha256"],
                )
            )
        version.artifacts.sort(key=lambda item: item.filename)
        if not version.artifacts:
            raise DiscoveryError("version has no valid artifact metadata")
        return version
