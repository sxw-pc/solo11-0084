"""Artifact size and SHA-256 acceptance plus quarantine handling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import IntegrityError, SecurityBoundaryError
from .util import (
    ensure_directory,
    resolve_under,
    safe_filename,
    safe_segment,
    sha256_file,
)


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


def read_verified_artifact(
    project_root: Path,
    artifact_root: Path,
    relative_path: str,
    expected_size: int,
    expected_sha256: str,
) -> tuple[bytes, "IntegrityReport"]:
    """Re-read an accepted artifact from disk and re-verify it before use.

    Ledger paths are relative to the project root; the resolved file must still
    live inside ``artifact_root`` so a tampered ledger row cannot redirect the
    reader outside the intake area.
    """
    if not isinstance(relative_path, str) or not relative_path:
        raise IntegrityError("invalid_path", "artifact path is missing")
    try:
        path = resolve_under(project_root, relative_path, "artifact local path")
    except SecurityBoundaryError as exc:
        raise IntegrityError("invalid_path", str(exc)) from exc
    allowed_root = artifact_root.resolve()
    if not path.is_relative_to(allowed_root):
        raise IntegrityError("invalid_path", f"artifact path escapes artifact root: {path}")
    try:
        report = verify_file(path, expected_size, expected_sha256)
    except IntegrityError as exc:
        raise IntegrityError("missing_file", f"artifact is not readable on disk: {path}") from exc
    if not report.accepted:
        raise IntegrityError(
            "reverify_mismatch",
            f"on-disk artifact no longer matches its accepted digest: {path}",
        )
    data = path.read_bytes()
    if len(data) != report.actual_size:
        raise IntegrityError("reverify_mismatch", f"artifact changed while reading: {path}")
    return data, report
