"""Chunk manifest validation and disk-truth chunk auditing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .util import is_sha256
from .verify import verify_chunk_region

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)\Z")


@dataclass(frozen=True)
class ChunkManifest:
    """Portal-declared fixed chunking: chunk size, per-chunk SHA-256, file SHA-256."""

    chunk_size: int
    digests: tuple[str, ...]
    total_size: int
    file_sha256: str

    @classmethod
    def build(
        cls, chunk_size: int, digests, total_size: int, file_sha256: str
    ) -> "ChunkManifest":
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise ValueError("chunk size must be an integer")
        if not 1 <= chunk_size <= 16_777_216:
            raise ValueError("chunk size is out of range")
        if not isinstance(total_size, int) or total_size < 1:
            raise ValueError("chunked artifacts must declare a positive size")
        if not is_sha256(file_sha256):
            raise ValueError("whole-file sha256 is invalid")
        if not isinstance(digests, (list, tuple)) or not digests:
            raise ValueError("chunk digest list is missing")
        normalized = []
        for digest in digests:
            if not is_sha256(digest):
                raise ValueError("chunk digest is not a SHA-256 hex string")
            normalized.append(digest.lower())
        expected = (total_size + chunk_size - 1) // chunk_size
        if len(normalized) != expected:
            raise ValueError(
                f"declared chunk count {len(normalized)} does not match "
                f"total size {total_size} at chunk size {chunk_size}"
            )
        return cls(chunk_size, tuple(normalized), total_size, file_sha256.lower())

    @property
    def chunk_count(self) -> int:
        return len(self.digests)

    def bounds(self, index: int) -> tuple[int, int]:
        """Return the [start, end) byte range of one declared chunk."""
        if not 0 <= index < self.chunk_count:
            raise IndexError(index)
        start = index * self.chunk_size
        return start, min(start + self.chunk_size, self.total_size)


def parse_chunk_attributes(attrs: dict, size: int, sha256: str) -> ChunkManifest | None:
    """Build a manifest from portal anchor attributes, or None when undeclared."""
    size_raw = attrs.get("data-chunk-size")
    digests_raw = attrs.get("data-chunk-sha256")
    if size_raw is None and digests_raw is None:
        return None
    if size_raw is None or digests_raw is None:
        raise ValueError("chunk metadata is incomplete")
    try:
        chunk_size = int(size_raw)
    except ValueError as exc:
        raise ValueError("chunk size is not an integer") from exc
    digests = tuple(part.strip() for part in digests_raw.split(",") if part.strip())
    return ChunkManifest.build(chunk_size, digests, size, sha256)


@dataclass(frozen=True)
class ChunkAudit:
    index: int
    start: int
    end: int
    expected_sha256: str
    actual_sha256: str | None
    ok: bool


def audit_file_chunks(path: Path, manifest: ChunkManifest) -> list[ChunkAudit] | None:
    """Re-hash every declared chunk region of ``path`` from disk (never trust state bits)."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > manifest.total_size:
        return None
    audits = []
    for index in range(manifest.chunk_count):
        start, end = manifest.bounds(index)
        result = verify_chunk_region(path, start, end - start, manifest.digests[index])
        audits.append(
            ChunkAudit(
                index, start, end, manifest.digests[index], result.actual_sha256, result.ok
            )
        )
    return audits


def parse_content_range(value: str) -> tuple[int, int, int] | None:
    match = _CONTENT_RANGE.fullmatch(value or "")
    if match is None or match.group(3) == "*":
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))
