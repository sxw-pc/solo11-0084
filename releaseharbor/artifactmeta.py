"""Parsing of per-artifact metadata attributes from portal version pages."""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urldefrag, urljoin

from .errors import SecurityBoundaryError
from .htmlparse import has_class
from .util import is_sha256, normalize_sha256, safe_filename
from .verify import parse_chunk_declaration

IssueSink = Callable[[str, str, dict], None]
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*\Z")


def parse_artifact_attrs(parsed, target_url: str, page_url: str, issue: IssueSink) -> dict | None:
    """Return validated anchor metadata for one artifact link, or None."""
    for anchor in parsed.anchors:
        if not has_class(anchor.attrs, "artifact-link"):
            continue
        resolved, _fragment = urldefrag(urljoin(page_url, anchor.attrs.get("href", "")))
        if resolved != target_url:
            continue
        name_raw = anchor.attrs.get("data-filename", "")
        try:
            name = safe_filename(name_raw)
        except SecurityBoundaryError as exc:
            issue("artifact.metadata_invalid", name_raw, {"reason": str(exc)})
            return None
        sha = anchor.attrs.get("data-sha256", "")
        if not is_sha256(sha):
            issue("artifact.metadata_invalid", name, {"reason": "invalid sha256"})
            return None
        size_raw = anchor.attrs.get("data-size", "")
        try:
            size = int(size_raw)
        except ValueError:
            issue("artifact.metadata_invalid", name, {"reason": "invalid size"})
            return None
        if size < 0 or size > 52_428_800:
            issue("artifact.metadata_invalid", name, {"reason": "size out of range"})
            return None
        media_type = anchor.attrs.get("data-media-type", "")
        if not _MEDIA_TYPE.fullmatch(media_type):
            issue("artifact.metadata_invalid", name, {"reason": "invalid media type"})
            return None
        chunk_size_raw = anchor.attrs.get("data-chunk-size", "")
        chunk_digests_raw = anchor.attrs.get("data-chunk-sha256", "")
        chunk_size = None
        chunk_digests: tuple[str, ...] = ()
        if chunk_size_raw or chunk_digests_raw:
            try:
                chunk_size, chunk_digests = parse_chunk_declaration(
                    size, chunk_size_raw, chunk_digests_raw
                )
            except ValueError as exc:
                issue("artifact.metadata_invalid", name, {"reason": str(exc)})
                return None
        return {
            "filename": name,
            "sha256": normalize_sha256(sha),
            "size": size,
            "media_type": media_type,
            "chunk_size": chunk_size,
            "chunk_sha256": chunk_digests,
        }
    return None
