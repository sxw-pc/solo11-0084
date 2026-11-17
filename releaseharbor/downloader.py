"""Resumable, size-bounded release artifact transfer."""

from __future__ import annotations

import http.client
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .discovery import ArtifactRef
from .errors import DownloadError, TransportError
from .httpclient import SafeHttpClient
from .util import ensure_directory, safe_filename, safe_segment

EventSink = Callable[[str, str, dict], None]
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)\Z")


def artifact_destination(
    artifact_root: Path, dataset_id: str, version_id: str, filename: str
) -> Path:
    return (
        artifact_root
        / safe_segment(dataset_id, "dataset id")
        / safe_segment(version_id, "version id")
        / safe_filename(filename)
    )


@dataclass
class DownloadResult:
    accepted: bool
    path: Path | None
    partial_path: Path
    attempts: int
    bytes_written: int
    resumed_from: int
    error_code: str | None = None
    error_detail: str | None = None


class _Retryable(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _CapacityExceeded(Exception):
    """The workspace capacity budget would be exceeded by this transfer."""


class Downloader:
    def __init__(
        self,
        config,
        client: SafeHttpClient,
        clock,
        on_event: EventSink | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.clock = clock
        self.on_event = on_event or (lambda code, subject, detail: None)

    def _emit(self, code: str, subject: str, detail: dict) -> None:
        self.on_event(code, subject, detail)

    def _size_of(self, path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def download(self, ref: ArtifactRef, part_limit: int | None = None) -> DownloadResult:
        target = artifact_destination(
            self.config.download.artifact_dir,
            ref.dataset_id,
            ref.version_id,
            ref.filename,
        )
        ensure_directory(target.parent)
        partial = target.with_name(target.name + ".part")
        expected = ref.expected_size
        maximum = self.config.download.max_artifact_bytes
        if expected > maximum:
            return DownloadResult(
                False, None, partial, 0, 0, 0, "artifact_too_large",
                f"declared size {expected} exceeds limit {maximum}",
            )
        offset = self._size_of(partial)
        if part_limit is not None and offset > part_limit:
            return DownloadResult(
                False, None, partial, 0, 0, 0, "capacity_exceeded",
                f"partial size {offset} exceeds workspace budget {part_limit}",
            )
        if offset > expected:
            partial.unlink(missing_ok=True)
            offset = 0
            self._emit("download.partial_discarded", ref.artifact_uid, {"reason": "oversize"})
        if expected == offset and offset > 0:
            os.replace(partial, target)
            self._emit("download.partial_completed", ref.artifact_uid, {"bytes": offset})
            return DownloadResult(True, target, partial, 0, offset, offset)
        resumed_from = offset
        max_attempts = self.config.download.retries + 1
        attempts = 0
        last_code = "download_failed"
        last_detail = "transfer did not complete"
        while attempts < max_attempts:
            attempts += 1
            if offset > expected:
                partial.unlink(missing_ok=True)
                offset = 0
            headers = {"Range": f"bytes={offset}-"} if offset > 0 else None
            try:
                result = self._attempt(
                    ref, target, partial, offset, expected, maximum, headers, attempts,
                    part_limit,
                )
                if result is not None:
                    return result
            except _CapacityExceeded as exc:
                return DownloadResult(
                    False, None, partial, attempts, self._size_of(partial), resumed_from,
                    "capacity_exceeded", str(exc),
                )
            except _Retryable as exc:
                last_code, last_detail = exc.code, exc.detail
            except DownloadError as exc:
                last_code, last_detail = "protocol_error", str(exc)
                break
            except (TransportError, OSError, http.client.HTTPException) as exc:
                last_code, last_detail = "transport_error", str(exc)
            offset = self._size_of(partial)
            if attempts < max_attempts:
                self._emit(
                    "download.retry",
                    ref.artifact_uid,
                    {"attempt": attempts, "offset": offset, "reason": last_detail},
                )
                self.clock.sleep(self.config.download.retry_backoff_ms * attempts / 1000.0)
        return DownloadResult(
            False, None, partial, attempts, offset, resumed_from, last_code, last_detail
        )

    def _attempt(
        self,
        ref: ArtifactRef,
        target: Path,
        partial: Path,
        offset: int,
        expected: int,
        maximum: int,
        headers: dict[str, str] | None,
        attempts: int,
        part_limit: int | None = None,
    ) -> DownloadResult | None:
        with self.client.open("GET", ref.url, headers=headers) as response:
            if response.status == 416:
                if offset == expected and offset > 0:
                    os.replace(partial, target)
                    return DownloadResult(True, target, partial, attempts, offset, offset)
                partial.unlink(missing_ok=True)
                raise _Retryable("range_not_satisfiable", "server rejected the partial range")
            if response.status in {429, 500, 502, 503, 504}:
                raise _Retryable("http_retryable", f"server returned HTTP {response.status}")
            if response.status not in {200, 206}:
                raise DownloadError(f"unexpected HTTP status {response.status}")
            if offset > 0 and response.status == 200:
                self._emit(
                    "download.range_ignored",
                    ref.artifact_uid,
                    {"requested_offset": offset},
                )
                offset = 0
            if response.status == 206:
                match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
                if match is None or int(match.group(1)) != offset:
                    partial.unlink(missing_ok=True)
                    raise _Retryable("range_mismatch", "server returned a mismatched byte range")
            length = response.content_length
            if length is not None and offset + length > maximum:
                partial.unlink(missing_ok=True)
                raise DownloadError("artifact exceeds the configured byte limit")
            if part_limit is not None and length is not None and offset + length > part_limit:
                raise _CapacityExceeded(
                    f"transfer would exceed workspace budget {part_limit}"
                )
            mode = "ab" if offset > 0 else "wb"
            written = offset
            with partial.open(mode) as handle:
                for chunk in response.iter_chunks(self.config.download.chunk_size):
                    written += len(chunk)
                    if part_limit is not None and written > part_limit:
                        raise _CapacityExceeded(
                            f"workspace budget {part_limit} exceeded during write"
                        )
                    if written > maximum or written > expected:
                        handle.close()
                        partial.unlink(missing_ok=True)
                        raise DownloadError("artifact exceeded its declared byte budget")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if written != expected:
                raise _Retryable(
                    "incomplete_transfer",
                    f"received {written} bytes, expected {expected}",
                )
            os.replace(partial, target)
            return DownloadResult(True, target, partial, attempts, written, offset)
