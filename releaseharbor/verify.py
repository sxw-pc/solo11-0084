"""Artifact size and SHA-256 acceptance plus quarantine handling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import IntegrityError
from .util import ensure_directory, safe_filename, safe_segment, sha256_file


@dataclass(frozen=True)
class IntegrityReport:
    actual_size: int
    actual_sha256: str
    size_ok: bool
    digest_ok: bool

    @property
    def accepted(self) -> bool:
        return self.size_ok and self.digest_ok


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


def require_accepted(path: Path, expected_size: int, expected_sha256: str) -> IntegrityReport:
    """Re-verify an accepted artifact on disk or raise a checkable error."""
    report = verify_file(path, expected_size, expected_sha256)
    if not report.accepted:
        failed = []
        if not report.size_ok:
            failed.append(f"size {report.actual_size} != expected {expected_size}")
        if not report.digest_ok:
            failed.append(f"sha256 {report.actual_sha256} != expected {expected_sha256.lower()}")
        raise IntegrityError(
            "reverify_failed", f"on-disk re-verification failed for {path}: {'; '.join(failed)}"
        )
    return report


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
