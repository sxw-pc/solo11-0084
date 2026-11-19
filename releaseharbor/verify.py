"""Artifact size and SHA-256 acceptance plus quarantine handling."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import IntegrityError
from .util import ensure_directory, normalize_sha256, safe_filename, safe_segment, sha256_file

MAX_CHUNK_SIZE = 52_428_800
MAX_CHUNK_COUNT = 65_536


@dataclass(frozen=True)
class IntegrityReport:
    actual_size: int
    actual_sha256: str
    size_ok: bool
    digest_ok: bool

    @property
    def accepted(self) -> bool:
        return self.size_ok and self.digest_ok


@dataclass(frozen=True)
class ChunkPlan:
    """Portal-declared fixed-size chunk digests for one artifact."""

    size: int
    chunk_size: int
    digests: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.digests)

    def bounds(self, index: int) -> tuple[int, int]:
        if not 0 <= index < self.count:
            raise IndexError(index)
        start = index * self.chunk_size
        return start, min(start + self.chunk_size, self.size)

    def length(self, index: int) -> int:
        start, end = self.bounds(index)
        return end - start

    def complete_count(self, length: int) -> int:
        """Number of declared chunks fully inside the first ``length`` bytes."""
        if length <= 0 or self.count == 0:
            return 0
        if length >= self.size:
            return self.count
        return length // self.chunk_size


def build_chunk_plan(size: int, chunk_size: int, digests) -> ChunkPlan:
    """Validate a declared chunk manifest against the declared total size."""
    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or not 1 <= chunk_size <= MAX_CHUNK_SIZE
    ):
        raise IntegrityError("chunk_manifest_invalid", f"chunk size {chunk_size!r} is out of range")
    normalized = []
    for item in digests:
        try:
            normalized.append(normalize_sha256(item))
        except (TypeError, ValueError) as exc:
            raise IntegrityError("chunk_manifest_invalid", f"invalid chunk digest: {exc}") from exc
    expected = size // chunk_size + (1 if size % chunk_size else 0)
    if len(normalized) != expected:
        raise IntegrityError(
            "chunk_manifest_invalid",
            f"declared {len(normalized)} chunk digests but size {size} with chunk size "
            f"{chunk_size} requires {expected}",
        )
    if len(normalized) > MAX_CHUNK_COUNT:
        raise IntegrityError("chunk_manifest_invalid", "chunk manifest is too large")
    return ChunkPlan(size=size, chunk_size=chunk_size, digests=tuple(normalized))


def parse_chunk_declaration(
    size: int, chunk_size_text: str, digests_text: str
) -> tuple[int, tuple[str, ...]]:
    """Parse portal page chunk attributes; ValueError when incomplete or inconsistent."""
    try:
        chunk_size = int(chunk_size_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid chunk size") from exc
    digests = tuple(part.strip() for part in digests_text.split(",") if part.strip())
    try:
        plan = build_chunk_plan(size, chunk_size, digests)
    except IntegrityError as exc:
        raise ValueError(exc.detail) from exc
    return plan.chunk_size, plan.digests


def hash_region(path: Path, start: int, length: int, chunk_size: int = 65536) -> str:
    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            block = handle.read(min(chunk_size, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def verify_chunk_region(path: Path, plan: ChunkPlan, index: int) -> bool:
    """Hash the on-disk bytes of one declared chunk and compare with the manifest."""
    start, end = plan.bounds(index)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < end:
        return False
    return hash_region(path, start, end - start) == plan.digests[index]


def verify_file(path: Path, expected_size: int, expected_sha256: str) -> IntegrityReport:
    if not path.is_file():
        raise IntegrityError("missing_file", f"artifact file is missing: {path}")
    actual_size = path.stat().st_size
    actual_sha256 = sha256_file(path)
    return IntegrityReport(
        actual_size=actual_size,
        actual_sha256=actual_sha256,
        size_ok=actual_size == expected_size,
        digest_ok=actual_sha256 == expected_sha256.lower(),
    )


def quarantine_file(
    source: Path,
    quarantine_root: Path,
    dataset_id: str,
    version_id: str,
    filename: str,
) -> Path:
    dataset_segment = safe_segment(dataset_id, "dataset id")
    version_segment = safe_segment(version_id, "version id")
    name = safe_filename(filename)
    destination = quarantine_root / dataset_segment / version_segment / name
    ensure_directory(destination.parent)
    if not source.is_file():
        raise IntegrityError("missing_file", f"cannot quarantine missing file: {source}")
    os.replace(source, destination)
    return destination
