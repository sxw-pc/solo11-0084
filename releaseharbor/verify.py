"""Artifact size and SHA-256 acceptance plus quarantine handling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import IntegrityError
from .util import ensure_directory, safe_filename, safe_segment, sha256_bytes, sha256_file


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
class ChunkVerification:
    actual_sha256: str | None
    ok: bool


def verify_chunk_region(
    path: Path, start: int, length: int, expected_sha256: str
) -> ChunkVerification:
    """Hash exactly ``length`` bytes of ``path`` at ``start`` and compare on disk truth."""
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(length)
    if len(data) != length:
        return ChunkVerification(actual_sha256=None, ok=False)
    actual = sha256_bytes(data)
    return ChunkVerification(actual_sha256=actual, ok=actual == expected_sha256.lower())


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
