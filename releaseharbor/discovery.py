"""Bounded discovery of datasets, versions, and release artifacts."""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urldefrag, urljoin

from .clock import Clock
from .coverage import (
    ACCEPTED,
    BUDGET_SKIPPED,
    CHANNEL_FILTERED,
    DUPLICATE,
    FETCH_FAILED,
    INVALID_LINK,
    CoverageTracker,
    PageEvidence,
)
from .errors import BudgetExceeded, DiscoveryError, SecurityBoundaryError, TransportError
from .htmlparse import has_class, parse_portal_html
from .httpclient import SafeHttpClient
from .models import ArtifactRef, DiscoveredDataset, DiscoveredVersion, DiscoveryResult
from .security import ValidatedTarget
from .util import is_sha256, normalize_sha256, safe_filename, safe_segment

EventSink = Callable[[str, str, dict], None]
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*\Z")


class Discovery:
    """Traverses only the fixture portal's documented link classes."""

    def __init__(self, client: SafeHttpClient, config, on_event: EventSink | None = None,
                 clock: Clock | None = None) -> None:
        self.client = client
        self.config = config
        self.on_event = on_event or (lambda code, subject, detail: None)
        self.clock = clock or Clock()
        self.pages_fetched = 0
        self.blocked: list[dict] = []
        self.issues: list[dict] = []
        self.tracker = CoverageTracker(self.clock, self.on_event)

    def _emit(self, code: str, subject: str, detail: dict) -> None:
        self.on_event(code, subject, detail)

    def _issue(self, code: str, subject: str, detail: dict) -> None:
        item = {"code": code, "subject": subject, **detail}
        self.issues.append(item)
        self._emit(code, subject, detail)

    def _load(self, url: str, level: str, parent_url: str | None) -> PageEvidence:
        if self.pages_fetched >= self.config.discovery.max_pages:
            self._emit(
                "discovery.page_budget",
                "page-budget",
                {"max_pages": self.config.discovery.max_pages},
            )
            raise BudgetExceeded("portal page budget exhausted")
        with self.client.open("GET", url) as response:
            self.pages_fetched += 1
            if response.status != 200:
                self._issue(
                    "page.failed", url, {"status": response.status, "reason": "unexpected status"}
                )
                raise DiscoveryError(f"unexpected status {response.status} for {url}")
            text = response.text(1_000_000)
        self._emit("page.fetched", url, {"status": 200, "bytes": len(text.encode("utf-8"))})
        return self.tracker.record_page(level, url, parent_url, text, self.pages_fetched)

    def _link(self, base_url: str, href: str) -> tuple[ValidatedTarget | None, str]:
        if not isinstance(href, str) or not href.strip():
            return None, "empty href"
        candidate, _fragment = urldefrag(urljoin(base_url, href.strip()))
        try:
            return self.client.validate(candidate), ""
        except SecurityBoundaryError as exc:
            self.blocked.append({"url": candidate, "reason": str(exc)})
            return None, f"outside the local portal boundary: {exc}"

    def _select(self, declaration, base_url: str, anchors, css_class: str, ref_attr: str,
                limit: int, event_code: str, subject: str):
        """Record invalid/duplicate/budget decisions; return targets to traverse."""
        links: dict[str, ValidatedTarget] = {}
        refs: dict[str, str] = {}
        for anchor in anchors:
            if not has_class(anchor.attrs, css_class):
                continue
            href = anchor.attrs.get("href", "")
            ref = anchor.attrs.get(ref_attr, "")
            target, reason = self._link(base_url, href)
            if target is None:
                self.tracker.decide(declaration, ref, href, INVALID_LINK, reason)
                continue
            if target.url in links:
                self.tracker.decide(declaration, ref, target.url, DUPLICATE, "duplicate target")
                continue
            links[target.url] = target
            refs[target.url] = ref
        ordered = [links[key] for key in sorted(links)]
        kept, dropped = ordered[:limit], ordered[limit:]
        if dropped:
            self._emit(event_code, subject, {"found": len(ordered), "kept": len(kept)})
            for target in dropped:
                self.tracker.decide(
                    declaration, refs[target.url], target.url, BUDGET_SKIPPED,
                    f"{event_code} limit {limit}",
                )
        return kept, refs

    def discover(self) -> DiscoveryResult:
        home = self.client.validate(self.config.portal.base_url).url
        evidence = self._load(home, "portal", None)
        page = parse_portal_html(evidence.text)
        declaration = self.tracker.begin("portal", "portal", evidence)
        kept, refs = self._select(
            declaration, home, page.anchors, "dataset-link", "data-dataset-id",
            self.config.discovery.max_datasets, "discovery.dataset_limit", "datasets",
        )
        datasets: list[DiscoveredDataset] = []
        seen_ids: set[str] = set()
        for target in kept:
            try:
                dataset = self._discover_dataset(target.url, home)
            except (DiscoveryError, TransportError, SecurityBoundaryError) as exc:
                self._issue("dataset.failed", target.url, {"reason": str(exc)})
                self.tracker.decide(declaration, refs[target.url], target.url, FETCH_FAILED, str(exc))
                continue
            if dataset.dataset_id in seen_ids:
                self.tracker.decide(
                    declaration, dataset.dataset_id, target.url, DUPLICATE, "dataset id seen"
                )
                continue
            seen_ids.add(dataset.dataset_id)
            datasets.append(dataset)
            self.tracker.decide(declaration, dataset.dataset_id, target.url, ACCEPTED)
        self.tracker.close(declaration)
        datasets.sort(key=lambda item: item.dataset_id)
        return DiscoveryResult(
            datasets, self.pages_fetched, self.blocked, self.issues,
            self.tracker.declarations, self.tracker.evidence,
        )

    def verify_stability(self) -> None:
        self.tracker.verify_stability(
            lambda decl: self._load(decl.page_url, decl.level, decl.parent_url)
        )
        self.tracker.attach_children()

    def _discover_dataset(self, page_url: str, home_url: str) -> DiscoveredDataset:
        evidence = self._load(page_url, "dataset", home_url)
        parsed = parse_portal_html(evidence.text)
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
        declaration = self.tracker.begin("dataset", dataset_id, evidence)
        kept, refs = self._select(
            declaration, page_url, parsed.anchors, "version-link", "data-version-id",
            self.config.discovery.max_versions, "discovery.version_limit", dataset_id,
        )
        seen_versions: set[str] = set()
        for target in kept:
            try:
                version = self._discover_version(dataset, target.url)
            except (DiscoveryError, TransportError, SecurityBoundaryError) as exc:
                self._issue("version.failed", target.url, {"reason": str(exc)})
                self.tracker.decide(declaration, refs[target.url], target.url, FETCH_FAILED, str(exc))
                continue
            if version.channel not in self.config.discovery.channels:
                self._emit(
                    "discovery.channel_filtered",
                    version.version_id,
                    {"channel": version.channel},
                )
                self.tracker.decide(
                    declaration, version.version_id, target.url, CHANNEL_FILTERED,
                    f"channel {version.channel}",
                )
                continue
            if version.version_id in seen_versions:
                self.tracker.decide(
                    declaration, version.version_id, target.url, DUPLICATE, "version id seen"
                )
                continue
            seen_versions.add(version.version_id)
            dataset.versions.append(version)
            self.tracker.decide(declaration, version.version_id, target.url, ACCEPTED)
        dataset.versions.sort(key=lambda item: item.version_id)
        self.tracker.close(declaration)
        return dataset

    def _discover_version(
        self, dataset: DiscoveredDataset, page_url: str
    ) -> DiscoveredVersion:
        evidence = self._load(page_url, "version", dataset.source_url)
        parsed = parse_portal_html(evidence.text)
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
        declaration = self.tracker.begin("version", version_id, evidence)
        kept, refs = self._select(
            declaration, page_url, parsed.anchors, "artifact-link", "data-filename",
            self.config.discovery.max_artifacts, "discovery.artifact_limit", version_id,
        )
        seen_names: set[str] = set()
        for target in kept:
            attributes, error = self._artifact_attrs(parsed, target.url, page_url)
            if attributes is None:
                self.tracker.decide(declaration, refs[target.url], target.url, INVALID_LINK, error)
                continue
            filename = attributes["filename"]
            if filename in seen_names:
                self.tracker.decide(declaration, filename, target.url, DUPLICATE, "filename seen")
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
                )
            )
            self.tracker.decide(declaration, filename, target.url, ACCEPTED)
        version.artifacts.sort(key=lambda item: item.filename)
        self.tracker.close(declaration)
        if not version.artifacts:
            raise DiscoveryError("version has no valid artifact metadata")
        return version

    def _artifact_attrs(self, parsed, target_url: str, page_url: str) -> tuple[dict | None, str]:
        for anchor in parsed.anchors:
            if not has_class(anchor.attrs, "artifact-link"):
                continue
            resolved, _ = urldefrag(urljoin(page_url, anchor.attrs.get("href", "")))
            if resolved != target_url:
                continue
            name_raw = anchor.attrs.get("data-filename", "")
            try:
                name = safe_filename(name_raw)
            except SecurityBoundaryError as exc:
                self._issue("artifact.metadata_invalid", name_raw, {"reason": str(exc)})
                return None, str(exc)
            sha = anchor.attrs.get("data-sha256", "")
            if not is_sha256(sha):
                self._issue("artifact.metadata_invalid", name, {"reason": "invalid sha256"})
                return None, "invalid sha256"
            size_raw = anchor.attrs.get("data-size", "")
            try:
                size = int(size_raw)
            except ValueError:
                self._issue("artifact.metadata_invalid", name, {"reason": "invalid size"})
                return None, "invalid size"
            if size < 0 or size > 52_428_800:
                self._issue("artifact.metadata_invalid", name, {"reason": "size out of range"})
                return None, "size out of range"
            media_type = anchor.attrs.get("data-media-type", "")
            if not _MEDIA_TYPE.fullmatch(media_type):
                self._issue("artifact.metadata_invalid", name, {"reason": "invalid media type"})
                return None, "invalid media type"
            return {
                "filename": name,
                "sha256": normalize_sha256(sha),
                "size": size,
                "media_type": media_type,
            }, ""
        return None, "artifact link metadata not found"
