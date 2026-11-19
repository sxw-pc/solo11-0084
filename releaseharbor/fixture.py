"""Synthetic portal fixture loading and deterministic content generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .errors import FixtureError, SecurityBoundaryError
from .util import (
    load_json_file,
    normalize_sha256,
    safe_filename,
    safe_segment,
    sha256_bytes,
)

_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*\Z")


@dataclass(frozen=True)
class ArtifactFixture:
    name: str
    media_type: str
    content: bytes
    actual_size: int
    actual_sha256: str
    declared_size: int
    declared_sha256: str
    transient_failures: int
    chunk_size: int | None
    declared_chunk_sha256: tuple[str, ...]
    range_extra_bytes: int
    range_total_delta: int
    range_content: bytes | None


@dataclass(frozen=True)
class VersionFixture:
    version_id: str
    label: str
    channel: str
    released_at: str
    notes: str
    artifacts: tuple[ArtifactFixture, ...]


@dataclass(frozen=True)
class DatasetFixture:
    dataset_id: str
    title: str
    summary: str
    publisher: str
    license: str
    versions: tuple[VersionFixture, ...]


@dataclass(frozen=True)
class PortalFixture:
    portal_title: str
    portal_description: str
    publisher: str
    datasets: tuple[DatasetFixture, ...]

    def artifact(self, dataset_id: str, version_id: str, filename: str) -> ArtifactFixture:
        for dataset in self.datasets:
            if dataset.dataset_id != dataset_id:
                continue
            for version in dataset.versions:
                if version.version_id != version_id:
                    continue
                for artifact in version.artifacts:
                    if artifact.name == filename:
                        return artifact
        raise KeyError((dataset_id, version_id, filename))


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise FixtureError(f"{label} must be an object")
    return value


def _keys(value: dict, label: str, required: set[str], optional: set[str] = frozenset()) -> None:
    missing = required - set(value)
    unknown = set(value) - required - set(optional)
    if missing:
        raise FixtureError(f"{label} missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise FixtureError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")


def _text(value, label: str, max_length: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise FixtureError(f"{label} must be a non-empty string")
    return value.strip()


def _safe_id(value, label: str) -> str:
    try:
        return safe_segment(value, label)
    except SecurityBoundaryError as exc:
        raise FixtureError(str(exc)) from exc


def _date_text(value, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise FixtureError(f"{label} must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise FixtureError(f"{label} is not a valid date") from exc
    return value


def _chunk_manifest(raw: dict, label: str, content: bytes) -> tuple[int | None, tuple[str, ...]]:
    chunk_size = raw.get("chunk_size")
    if chunk_size is None:
        if "chunk_sha256" in raw:
            raise FixtureError(f"{label}.chunk_sha256 requires chunk_size")
        return None, ()
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 1 <= chunk_size <= 8_388_608:
        raise FixtureError(f"{label}.chunk_size is invalid")
    count = len(content) // chunk_size + (1 if len(content) % chunk_size else 0)
    if "chunk_sha256" in raw:
        items = raw["chunk_sha256"]
        if not isinstance(items, list):
            raise FixtureError(f"{label}.chunk_sha256 must be a list")
        try:
            declared = tuple(normalize_sha256(item) for item in items)
        except ValueError as exc:
            raise FixtureError(f"{label}.chunk_sha256 is invalid") from exc
        if len(declared) != count:
            raise FixtureError(f"{label}.chunk_sha256 count does not match content")
        return chunk_size, declared
    digests = tuple(
        sha256_bytes(content[index * chunk_size : (index + 1) * chunk_size])
        for index in range(count)
    )
    return chunk_size, digests


def _range_faults(raw: dict, label: str) -> tuple[int, int, bytes | None]:
    extra = raw.get("range_extra_bytes", 0)
    if isinstance(extra, bool) or not isinstance(extra, int) or not 0 <= extra <= 16:
        raise FixtureError(f"{label}.range_extra_bytes is invalid")
    delta = raw.get("range_total_delta", 0)
    if isinstance(delta, bool) or not isinstance(delta, int) or not -16 <= delta <= 16:
        raise FixtureError(f"{label}.range_total_delta is invalid")
    swapped_raw = raw.get("range_content")
    swapped = None
    if swapped_raw is not None:
        if not isinstance(swapped_raw, str):
            raise FixtureError(f"{label}.range_content must be a string")
        swapped = swapped_raw.encode("utf-8")
        if len(swapped) > 65_536:
            raise FixtureError(f"{label}.range_content is too large for the fixture")
    return extra, delta, swapped


def _artifact(raw, label: str) -> ArtifactFixture:
    _keys(
        raw,
        label,
        {"name", "media_type", "content", "sha256"},
        {
            "declared_size",
            "transient_failures",
            "chunk_size",
            "chunk_sha256",
            "range_extra_bytes",
            "range_total_delta",
            "range_content",
        },
    )
    try:
        name = safe_filename(raw["name"])
    except SecurityBoundaryError as exc:
        raise FixtureError(str(exc)) from exc
    media_type = _text(raw["media_type"], f"{label}.media_type", 100)
    if not _MEDIA_TYPE.fullmatch(media_type):
        raise FixtureError(f"{label}.media_type is invalid")
    content_raw = raw["content"]
    if not isinstance(content_raw, str):
        raise FixtureError(f"{label}.content must be a string")
    content = content_raw.encode("utf-8")
    if len(content) > 65_536:
        raise FixtureError(f"{label}.content is too large for the fixture")
    try:
        declared_sha = normalize_sha256(raw["sha256"])
    except ValueError as exc:
        raise FixtureError(f"{label}.sha256 is invalid") from exc
    size = raw.get("declared_size", len(content))
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 8_388_608:
        raise FixtureError(f"{label}.declared_size is invalid")
    failures = raw.get("transient_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or not 0 <= failures <= 5:
        raise FixtureError(f"{label}.transient_failures is invalid")
    chunk_size, chunk_digests = _chunk_manifest(raw, label, content)
    extra, delta, swapped = _range_faults(raw, label)
    return ArtifactFixture(
        name=name,
        media_type=media_type,
        content=content,
        actual_size=len(content),
        actual_sha256=sha256_bytes(content),
        declared_size=size,
        declared_sha256=declared_sha,
        transient_failures=failures,
        chunk_size=chunk_size,
        declared_chunk_sha256=chunk_digests,
        range_extra_bytes=extra,
        range_total_delta=delta,
        range_content=swapped,
    )


def _version(raw, label: str) -> VersionFixture:
    _keys(raw, label, {"id", "label", "channel", "released_at", "artifacts"}, {"notes"})
    artifacts_raw = raw["artifacts"]
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise FixtureError(f"{label}.artifacts must be a non-empty list")
    artifacts = tuple(
        _artifact(_object(item, f"{label}.artifacts[{index}]"), f"{label}.artifacts[{index}]")
        for index, item in enumerate(artifacts_raw)
    )
    names = [item.name for item in artifacts]
    if len(set(names)) != len(names):
        raise FixtureError(f"{label} contains duplicate artifact names")
    return VersionFixture(
        version_id=_safe_id(raw["id"], f"{label}.id"),
        label=_text(raw["label"], f"{label}.label", 120),
        channel=_safe_id(raw["channel"], f"{label}.channel"),
        released_at=_date_text(raw["released_at"], f"{label}.released_at"),
        notes=_text(raw.get("notes", "No release notes supplied."), f"{label}.notes", 1000),
        artifacts=artifacts,
    )


def _dataset(raw, label: str) -> DatasetFixture:
    _keys(
        raw,
        label,
        {"id", "title", "summary", "publisher", "license", "versions"},
    )
    versions_raw = raw["versions"]
    if not isinstance(versions_raw, list) or not versions_raw:
        raise FixtureError(f"{label}.versions must be a non-empty list")
    versions = tuple(
        _version(_object(item, f"{label}.versions[{index}]"), f"{label}.versions[{index}]")
        for index, item in enumerate(versions_raw)
    )
    ids = [item.version_id for item in versions]
    if len(set(ids)) != len(ids):
        raise FixtureError(f"{label} contains duplicate version ids")
    return DatasetFixture(
        dataset_id=_safe_id(raw["id"], f"{label}.id"),
        title=_text(raw["title"], f"{label}.title", 200),
        summary=_text(raw["summary"], f"{label}.summary", 1000),
        publisher=_text(raw["publisher"], f"{label}.publisher", 200),
        license=_text(raw["license"], f"{label}.license", 100),
        versions=versions,
    )


def load_portal_fixture(path: Path) -> PortalFixture:
    try:
        raw = load_json_file(path, limit=1_000_000)
    except (OSError, ValueError) as exc:
        raise FixtureError(f"cannot read portal fixture {path}: {exc}") from exc
    raw = _object(raw, "fixture")
    _keys(
        raw,
        "fixture",
        {"schema_version", "portal_title", "portal_description", "publisher", "datasets"},
    )
    if raw["schema_version"] != 1:
        raise FixtureError("fixture schema_version must be 1")
    datasets_raw = raw["datasets"]
    if not isinstance(datasets_raw, list) or not 1 <= len(datasets_raw) <= 30:
        raise FixtureError("fixture.datasets must contain 1-30 datasets")
    datasets = tuple(
        _dataset(_object(item, f"fixture.datasets[{index}]"), f"fixture.datasets[{index}]")
        for index, item in enumerate(datasets_raw)
    )
    ids = [item.dataset_id for item in datasets]
    if len(set(ids)) != len(ids):
        raise FixtureError("fixture contains duplicate dataset ids")
    return PortalFixture(
        portal_title=_text(raw["portal_title"], "fixture.portal_title", 200),
        portal_description=_text(
            raw["portal_description"], "fixture.portal_description", 1000
        ),
        publisher=_text(raw["publisher"], "fixture.publisher", 200),
        datasets=datasets,
    )
