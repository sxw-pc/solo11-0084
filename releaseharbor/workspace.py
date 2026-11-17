"""Cleanup and preparation of configured, generated output locations."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import ConfigError

if TYPE_CHECKING:
    from .config import DownloadSettings


def validate_layout(
    source: Path, fixture: Path, download: DownloadSettings, registry: Path | None = None
) -> None:
    """Reject output layouts that would contain or overwrite input files."""
    work = download.work_dir.resolve()
    for path, label in (
        (download.artifact_dir, "artifact_dir"),
        (download.quarantine_dir, "quarantine_dir"),
        (download.db_path, "db_path"),
    ):
        resolved = path.resolve()
        if resolved == work or not resolved.is_relative_to(work):
            raise ConfigError(f"download.{label} must live below download.work_dir")
    protected = (source, fixture) if registry is None else (source, fixture, registry)
    for output in (
        download.work_dir,
        download.artifact_dir,
        download.quarantine_dir,
        download.report_dir,
        download.db_path,
    ):
        for item in protected:
            resolved_output = output.resolve()
            resolved_input = item.resolve()
            if resolved_output == resolved_input or resolved_input.is_relative_to(
                resolved_output
            ):
                raise ConfigError(f"generated output path would contain an input file: {output}")


def _safe_output(path: Path, root: Path, protected: tuple[Path, ...]) -> None:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"output path escapes project root: {path}") from exc
    if relative == Path("."):
        raise ConfigError("an output path may not be the project root")
    for item in protected:
        resolved = item.resolve(strict=False)
        if resolved == path.resolve(strict=False) or resolved.is_relative_to(
            path.resolve(strict=False)
        ):
            raise ConfigError(f"output path would contain a protected input: {path}")


def reset_outputs(config) -> None:
    settings = config.download
    directories = (settings.artifact_dir, settings.quarantine_dir, settings.report_dir)
    protected = (config.source_path, config.portal.fixture)
    for directory in directories:
        _safe_output(directory, config.root, protected)
        if directory.exists():
            if not directory.is_dir():
                raise ConfigError(f"expected an output directory: {directory}")
            shutil.rmtree(directory)
    _safe_output(settings.db_path, config.root, protected)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = settings.db_path.with_name(settings.db_path.name + suffix)
        candidate.unlink(missing_ok=True)
