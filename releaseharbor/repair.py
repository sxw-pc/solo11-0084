"""Chunk-level audit, staged local repair, and the bounded recovery engine."""

from __future__ import annotations

import http.client
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .discovery import ArtifactRef
from .errors import DownloadError, TransportError
from .verify import ChunkPlan, hash_region, verify_chunk_region

EventSink = Callable[[str, str, dict], None]
ChunkStateSink = Callable[[str, int, str, str], None]
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)\Z")


class TransferRetryable(Exception):
    """A transfer or repair attempt failed but may be retried within bounds."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ChunkRepairError(TransferRetryable):
    """One repair attempt failed; ``received`` counts bytes already downloaded."""

    def __init__(self, code: str, detail: str, received: int = 0) -> None:
        super().__init__(code, detail)
        self.received = received


@dataclass
class RecoveryOutcome:
    accepted: bool
    offset: int
    transfers: int
    chunks_repaired: int
    recovery_bytes: int
    error_code: str | None = None
    error_detail: str | None = None


def chunk_temp_path(partial: Path, index: int) -> Path:
    return partial.with_name(f"{partial.name}.chunk-{index}.tmp")


def stale_chunk_temps(partial: Path) -> list[Path]:
    if not partial.parent.is_dir():
        return []
    return sorted(partial.parent.glob(partial.name + ".chunk-*.tmp"))


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class ChunkRepairer:
    """Verifies declared chunks on disk and re-fetches single chunks via Range."""

    def __init__(
        self,
        client,
        config,
        clock=None,
        on_event: EventSink | None = None,
        on_chunk_state: ChunkStateSink | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.clock = clock
        self.on_event = on_event or (lambda code, subject, detail: None)
        self.on_chunk_state = on_chunk_state or (lambda uid, index, digest, status: None)

    def _emit(self, code: str, subject: str, detail: dict) -> None:
        self.on_event(code, subject, detail)

    def _sleep(self, seconds: float) -> None:
        if self.clock is not None and seconds > 0:
            self.clock.sleep(seconds)

    def audit(self, path: Path, plan: ChunkPlan, length: int) -> list[int]:
        """Indices of complete chunks whose disk bytes fail the declared digest."""
        bad = []
        for index in range(plan.complete_count(length)):
            if not verify_chunk_region(path, plan, index):
                bad.append(index)
        return bad

    def discard_partial(self, partial: Path) -> None:
        partial.unlink(missing_ok=True)
        self.clean_temps(partial)

    def clean_temps(self, partial: Path) -> None:
        for temp in stale_chunk_temps(partial):
            temp.unlink(missing_ok=True)

    def has_valid_staged(self, partial: Path, plan: ChunkPlan, index: int) -> bool:
        temp = chunk_temp_path(partial, index)
        if not temp.is_file() or temp.stat().st_size != plan.length(index):
            return False
        return hash_region(temp, 0, plan.length(index)) == plan.digests[index]

    def note_finalized(self, ref: ArtifactRef, plan: ChunkPlan) -> None:
        for index in range(plan.count):
            self.on_chunk_state(ref.artifact_uid, index, plan.digests[index], "accepted")

    def repair_chunk(self, ref: ArtifactRef, partial: Path, plan: ChunkPlan, index: int) -> int:
        """Restore one chunk in ``partial``; returns bytes fetched from the network."""
        start, _end = plan.bounds(index)
        temp = chunk_temp_path(partial, index)
        if self.has_valid_staged(partial, plan, index):
            self._splice(partial, temp, start)
            self._accepted(ref, plan, index, "staged")
            return 0
        temp.unlink(missing_ok=True)
        received = self._fetch(ref, plan, index, temp)
        actual = hash_region(temp, 0, plan.length(index))
        if actual != plan.digests[index]:
            temp.unlink(missing_ok=True)
            raise ChunkRepairError(
                "chunk_digest_mismatch",
                f"chunk {index} digest {actual} does not match declared {plan.digests[index]}",
                received,
            )
        self._splice(partial, temp, start)
        self._accepted(ref, plan, index, "range")
        return received

    def recover(self, ref, target, partial, plan, offset, expected, maximum, stream_into):
        """Drive the bounded audit/repair/transfer loop until acceptance or failure."""
        contract = self.config.download.recovery_contract
        budget = self.config.download.max_recovery_bytes
        max_chunk_attempts = self.config.download.chunk_retries + 1
        max_transfers = self.config.download.retries + 1
        chunk_attempts: dict[int, int] = {}
        recovery_bytes = 0
        transfers = 0
        repairs = 0
        last_code = "download_failed"
        last_detail = "transfer did not complete"
        uid = ref.artifact_uid

        def failure(code: str, detail: str) -> RecoveryOutcome:
            return RecoveryOutcome(False, _size_of(partial), transfers, repairs, recovery_bytes, code, detail)

        while True:
            bad = self.audit(partial, plan, offset)
            detail = {"offset": offset, "complete": plan.complete_count(offset), "bad": bad}
            self._emit("download.chunk_audit", uid, detail)
            if not bad and offset == expected:
                self.note_finalized(ref, plan)
                self.clean_temps(partial)
                os.replace(partial, target)
                self._emit("download.partial_completed", uid, {"bytes": offset})
                return RecoveryOutcome(True, offset, transfers, repairs, recovery_bytes)
            if bad and contract == "conservative":
                remaining = budget - recovery_bytes
                if expected > remaining:
                    return failure(
                        "recovery_budget_exceeded",
                        f"conservative re-download needs {expected} bytes, budget has {remaining}",
                    )
                self._emit("download.contract_discard", uid, {"contract": contract, "bad_chunks": bad})
                self.discard_partial(partial)
                offset = 0
                recovery_bytes += expected
                last_code = "chunk_audit_failed"
                last_detail = f"{len(bad)} on-disk chunk(s) failed the declared digests"
            elif bad:
                for index in bad:
                    if chunk_attempts.get(index, 0) >= max_chunk_attempts:
                        return failure(
                            "chunk_repair_failed",
                            f"chunk {index} rejected after {max_chunk_attempts} attempt(s): "
                            f"{last_detail}",
                        )
                    staged = self.has_valid_staged(partial, plan, index)
                    if not staged and recovery_bytes + plan.length(index) > budget:
                        return failure(
                            "recovery_budget_exceeded",
                            f"repairing chunk {index} needs {plan.length(index)} bytes, "
                            f"budget has {budget - recovery_bytes}",
                        )
                    chunk_attempts[index] = chunk_attempts.get(index, 0) + 1
                    try:
                        got = self.repair_chunk(ref, partial, plan, index)
                    except ChunkRepairError as exc:
                        recovery_bytes += exc.received
                        last_code, last_detail = exc.code, exc.detail
                        detail = {
                            "index": index,
                            "attempt": chunk_attempts[index],
                            "reason": exc.detail,
                        }
                        self._emit("download.chunk_retry", uid, detail)
                        self._sleep(
                            self.config.download.retry_backoff_ms * chunk_attempts[index] / 1000.0
                        )
                        break
                    except (TransportError, OSError, http.client.HTTPException) as exc:
                        last_code, last_detail = "transport_error", str(exc)
                        self._sleep(self.config.download.retry_backoff_ms / 1000.0)
                        break
                    recovery_bytes += got
                    repairs += 1
                continue
            if transfers >= max_transfers:
                return failure(last_code, last_detail)
            transfers += 1
            headers = {"Range": f"bytes={offset}-"} if offset > 0 else None
            try:
                offset = stream_into(ref, partial, offset, expected, maximum, headers)
            except TransferRetryable as exc:
                last_code, last_detail = exc.code, exc.detail
            except DownloadError as exc:
                return failure("protocol_error", str(exc))
            except (TransportError, OSError, http.client.HTTPException) as exc:
                last_code, last_detail = "transport_error", str(exc)
            else:
                continue
            offset = _size_of(partial)
            detail = {"attempt": transfers, "offset": offset, "reason": last_detail}
            self._emit("download.retry", uid, detail)
            self._sleep(self.config.download.retry_backoff_ms * transfers / 1000.0)

    def _accepted(self, ref: ArtifactRef, plan: ChunkPlan, index: int, source: str) -> None:
        digest = plan.digests[index]
        self.on_chunk_state(ref.artifact_uid, index, digest, "accepted")
        detail = {"index": index, "sha256": digest, "source": source}
        self._emit("download.chunk_accepted", ref.artifact_uid, detail)

    def _splice(self, partial: Path, temp: Path, start: int) -> None:
        with temp.open("rb") as source, partial.open("r+b") as handle:
            handle.seek(start)
            for block in iter(lambda: source.read(65536), b""):
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        temp.unlink()

    def _fetch(self, ref: ArtifactRef, plan: ChunkPlan, index: int, temp: Path) -> int:
        start, end = plan.bounds(index)
        length = end - start
        detail = {"index": index, "start": start, "end": end - 1}
        self._emit("download.chunk_repair", ref.artifact_uid, detail)
        headers = {"Range": f"bytes={start}-{end - 1}"}
        with self.client.open("GET", ref.url, headers=headers) as response:
            if response.status in {429, 500, 502, 503, 504}:
                raise ChunkRepairError("http_retryable", f"server returned HTTP {response.status}")
            if response.status != 206:
                raise ChunkRepairError("range_refused", f"repair returned HTTP {response.status}")
            match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
            if match is None:
                raise ChunkRepairError("range_mismatch", "repair response is missing Content-Range")
            actual_start, actual_end = int(match.group(1)), int(match.group(2))
            if actual_start != start or actual_end != end - 1:
                raise ChunkRepairError(
                    "range_mismatch",
                    f"repair returned bytes {actual_start}-{actual_end}, expected {start}-{end - 1}",
                )
            total_raw = match.group(3)
            if total_raw == "*" or int(total_raw) != plan.size:
                raise ChunkRepairError(
                    "range_total_mismatch",
                    f"repair total {total_raw} does not match declared {plan.size}",
                )
            received = 0
            try:
                with temp.open("wb") as handle:
                    for block in response.iter_chunks(self.config.download.chunk_size):
                        received += len(block)
                        if received > length:
                            raise ChunkRepairError(
                                "range_length_mismatch",
                                f"repair sent more than the declared {length} bytes",
                                received,
                            )
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            except ChunkRepairError:
                temp.unlink(missing_ok=True)
                raise
            if received != length:
                temp.unlink(missing_ok=True)
                raise ChunkRepairError(
                    "range_length_mismatch",
                    f"repair sent {received} bytes, expected {length}",
                    received,
                )
        return received
