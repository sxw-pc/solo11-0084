"""Small deterministic, filesystem-safe helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .errors import SecurityBoundaryError

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}\Z")
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL"}
_RESERVED.update(f"COM{i}" for i in range(1, 10))
_RESERVED.update(f"LPT{i}" for i in range(1, 10))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 65536) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(*parts: str, length: int = 24) -> str:
    text = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(text).hexdigest()[:length]


def _reject_reserved(value: str) -> None:
    stem = value.split(".", 1)[0].upper()
    if stem in _RESERVED:
        raise SecurityBoundaryError(f"reserved filename component: {value!r}")


def safe_filename(value: str) -> str:
    """Return a single safe artifact filename or reject it."""
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise SecurityBoundaryError(f"unsafe artifact filename: {value!r}")
    if value in {".", ".."} or value.endswith((".", " ")):
        raise SecurityBoundaryError(f"unsafe artifact filename: {value!r}")
    if "/" in value or "\\" in value or "\x00" in value or ":" in value:
        raise SecurityBoundaryError(f"unsafe artifact filename: {value!r}")
    _reject_reserved(value)
    return value


def safe_segment(value: str, label: str = "identifier") -> str:
    """Return one safe path segment for a dataset or version identifier."""
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise SecurityBoundaryError(f"unsafe {label}: {value!r}")
    if value.endswith((".", " ")):
        raise SecurityBoundaryError(f"unsafe {label}: {value!r}")
    _reject_reserved(value)
    return value


def resolve_under(root: Path, value: str, label: str, *, allow_root: bool = False) -> Path:
    """Resolve a path and require it to remain below ``root``."""
    root_resolved = root.resolve()
    raw = Path(value)
    candidate = raw if raw.is_absolute() else root_resolved / raw
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SecurityBoundaryError(f"{label} escapes the project root: {value!r}") from exc
    if not allow_root and relative == Path("."):
        raise SecurityBoundaryError(f"{label} must not be the project root")
    return resolved


def relative_posix(root: Path, path: Path) -> str:
    root_resolved = root.resolve()
    try:
        return path.resolve(strict=False).relative_to(root_resolved).as_posix()
    except ValueError as exc:
        raise SecurityBoundaryError(f"path is outside the project root: {path}") from exc


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise OSError(f"not a directory: {path}")


def atomic_write_text(path: Path, text: str) -> None:
    ensure_directory(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_text_limited(path: Path, limit: int = 1_000_000) -> str:
    size = path.stat().st_size
    if size > limit:
        raise ValueError(f"file exceeds {limit} bytes: {path}")
    return path.read_text(encoding="utf-8")


def load_json_file(path: Path, limit: int = 1_000_000) -> Any:
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(read_text_limited(path, limit))


def is_sha256(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value)


def normalize_sha256(value: str) -> str:
    if not is_sha256(value):
        raise ValueError("SHA-256 must contain exactly 64 hexadecimal characters")
    return value.lower()


def human_bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KiB"
    return f"{count / (1024 * 1024):.1f} MiB"
