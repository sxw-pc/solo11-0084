"""Cleanup and preparation of configured, generated output locations."""

from __future__ import annotations

import shutil
from pathlib import Path

from .errors import ConfigError


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
