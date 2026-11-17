"""Bounded discovery of datasets, versions, and release artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urldefrag, urljoin

from .errors import DiscoveryError, SecurityBoundaryError, TransportError
from .htmlparse import has_class, parse_portal_html
from .httpclient import SafeHttpClient
from .security import ValidatedTarget
from .util import is_sha256, normalize_sha256, safe_filename, safe_segment, stable_id

EventSink = Callable[[str, str, dict], None]
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*\Z")
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


def portal_license_evidence(dataset: DiscoveredDataset) -> dict:
    """Describe portal license text as non-executable provenance only.

    The captured string remains a human-readable label for the intake report;
    combination adjudication never treats it as an executable rule. Executable
    fictional rules come exclusively from the controlled local rule table.
    """
    return {
        "dataset_id": dataset.dataset_id,
        "portal_license_text": dataset.license,
        "executable": False,
        "rule_source": "controlled-local-rule-table",
    }


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

    def _collect_links(self, parsed, class_name: str, base_url: str) -> list[ValidatedTarget]:
        links: dict[str, ValidatedTarget] = {}
        for anchor in parsed.anchors:
            if not has_class(anchor.attrs, class_name):
                continue
            target = self._link(base_url, anchor.attrs.get("href", ""))
            if target is not None:
                links[target.url] = target
        return [links[key] for key in sorted(links)]

    def _cap_links(self, links, limit: int, code: str, subject: str) -> list:
        if len(links) <= limit:
            return links
        self._emit(code, subject, {"found": len(links), "kept": limit})
        return links[:limit]

    def discover(self) -> DiscoveryResult:
        home = self.client.validate(self.config.portal.base_url).url
        page = parse_portal_html(self._load(home))
        ordered = self._cap_links(
            self._collect_links(page, "dataset-link", home),
            self.config.discovery.max_datasets,
            "discovery.dataset_limit",
            "datasets",
        )
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
        ordered = self._cap_links(
            self._collect_links(parsed, "version-link", page_url),
            self.config.discovery.max_versions,
            "discovery.version_limit",
            dataset.dataset_id,
        )
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
        ordered = self._cap_links(
            self._collect_links(parsed, "artifact-link", page_url),
            self.config.discovery.max_artifacts,
            "discovery.artifact_limit",
            version_id,
        )
        seen_names: set[str] = set()
        for target in ordered:
            attributes = self._artifact_attrs(parsed, target.url, page_url)
            if attributes is None:
                continue
            filename = attributes["filename"]
            if filename in seen_names:
                continue
            seen_names.add(filename)
            version.artifacts.append(
                ArtifactRef(
                    dataset.dataset_id, dataset.title, version_id, label, channel,
                    released, filename, target.url, attributes["media_type"],
                    attributes["size"], attributes["sha256"],
                )
            )
        version.artifacts.sort(key=lambda item: item.filename)
        if not version.artifacts:
            raise DiscoveryError("version has no valid artifact metadata")
        return version

    def _artifact_attrs(self, parsed, target_url: str, page_url: str) -> dict | None:
        for anchor in parsed.anchors:
            if not has_class(anchor.attrs, "artifact-link"):
                continue
            resolved, _ = urldefrag(urljoin(page_url, anchor.attrs.get("href", "")))
            if resolved != target_url:
                continue

            def invalid(reason: str) -> None:
                self._issue("artifact.metadata_invalid", anchor.attrs.get("data-filename", ""),
                            {"reason": reason})

            try:
                name = safe_filename(anchor.attrs.get("data-filename", ""))
            except SecurityBoundaryError as exc:
                invalid(str(exc))
                return None
            sha = anchor.attrs.get("data-sha256", "")
            if not is_sha256(sha):
                invalid("invalid sha256")
                return None
            try:
                size = int(anchor.attrs.get("data-size", ""))
            except ValueError:
                invalid("invalid size")
                return None
            if size < 0 or size > 52_428_800:
                invalid("size out of range")
                return None
            media_type = anchor.attrs.get("data-media-type", "")
            if not _MEDIA_TYPE.fullmatch(media_type):
                invalid("invalid media type")
                return None
            return {
                "filename": name,
                "sha256": normalize_sha256(sha),
                "size": size,
                "media_type": media_type,
            }
        return None
