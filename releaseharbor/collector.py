"""Per-artifact metadata collection, transfer, and integrity acceptance."""

from __future__ import annotations

from pathlib import Path

from .discovery import ArtifactRef
from .downloader import Downloader, artifact_destination
from .errors import CapacityAdmissionError, IntegrityError, SecurityBoundaryError, TransportError
from .httpclient import SafeHttpClient
from .ledger import Ledger
from .util import relative_posix
from .verify import quarantine_file, verify_file


class ArtifactProcessor:
    def __init__(
        self,
        config,
        client: SafeHttpClient,
        ledger: Ledger,
        downloader: Downloader,
        run_id: str,
    ) -> None:
        self.config = config
        self.client = client
        self.ledger = ledger
        self.downloader = downloader
        self.run_id = run_id

    def _event(self, code: str, ref: ArtifactRef, detail: dict, level: str = "info") -> None:
        self.ledger.record_event(self.run_id, code, ref.artifact_uid, detail, level)

    def _base_detail(self, ref: ArtifactRef) -> dict:
        return {
            "dataset": ref.dataset_id,
            "version": ref.version_id,
            "filename": ref.filename,
        }

    def _fail(self, ref: ArtifactRef, code: str, detail: str) -> None:
        self.ledger.update_artifact(
            ref.artifact_uid,
            self.run_id,
            status="failed",
            local_path=None,
            error_code=code,
            error_detail=detail,
        )
        payload = self._base_detail(ref)
        payload.update({"code": code, "detail": detail})
        self._event("artifact.failed", ref, payload, "warning")

    def _capacity_blocked(self, ref: ArtifactRef, code: str, detail: str) -> str:
        """Record a capacity rejection; never confused with source validation."""
        self.ledger.update_artifact(
            ref.artifact_uid,
            self.run_id,
            status="capacity_blocked",
            error_code=code,
            error_detail=detail,
        )
        payload = self._base_detail(ref)
        payload.update({"code": code, "detail": detail})
        self._event("capacity.rejected", ref, payload, "warning")
        return "capacity_blocked"

    def process(self, ref: ArtifactRef, capacity=None) -> str:
        row = self.ledger.upsert_artifact(self.run_id, ref)
        maximum = self.config.download.max_artifact_bytes
        if ref.expected_size > maximum:
            self._fail(
                ref,
                "artifact_too_large",
                f"declared size {ref.expected_size} exceeds limit {maximum}",
            )
            return "failed"
        try:
            with self.client.open("HEAD", ref.url) as response:
                remote_size = response.content_length
                remote_type = response.headers.get("content-type", "")
                remote_etag = response.headers.get("etag", "")
                accept_ranges = response.headers.get("accept-ranges", "")
                status = response.status
        except (TransportError, SecurityBoundaryError) as exc:
            self._fail(ref, "metadata_unreachable", str(exc))
            return "failed"
        self.ledger.update_artifact(
            ref.artifact_uid,
            self.run_id,
            remote_size=remote_size,
            remote_media_type=remote_type or None,
            remote_etag=remote_etag or None,
            accept_ranges=accept_ranges or None,
        )
        payload = self._base_detail(ref)
        payload.update(
            {
                "status": status,
                "remote_size": remote_size,
                "media_type": remote_type or None,
                "accept_ranges": accept_ranges or None,
            }
        )
        self._event("artifact.metadata", ref, payload)
        if status != 200:
            self._fail(ref, "metadata_http_status", f"HEAD returned HTTP {status}")
            return "failed"
        if remote_size is not None and remote_size != ref.expected_size:
            self._fail(
                ref,
                "remote_size_mismatch",
                f"HEAD size {remote_size} differs from declared {ref.expected_size}",
            )
            return "failed"
        if remote_size is not None and remote_size > maximum:
            self._fail(ref, "artifact_too_large", f"remote size {remote_size} exceeds limit")
            return "failed"

        target = artifact_destination(
            self.config.download.artifact_dir,
            ref.dataset_id,
            ref.version_id,
            ref.filename,
        )
        if row["status"] in {"verified", "skipped"} and target.is_file():
            try:
                existing = verify_file(target, ref.expected_size, ref.expected_sha256)
            except IntegrityError:
                existing = None
            if existing is not None and existing.accepted:
                self.ledger.update_artifact(
                    ref.artifact_uid,
                    self.run_id,
                    status="skipped",
                    local_path=relative_posix(self.config.root, target),
                    actual_size=existing.actual_size,
                    actual_sha256=existing.actual_sha256,
                    error_code=None,
                    error_detail=None,
                    quarantine_path=None,
                )
                payload = self._base_detail(ref)
                payload.update(
                    {
                        "local_path": relative_posix(self.config.root, target),
                        "sha256": existing.actual_sha256,
                    }
                )
                self._event("artifact.skipped", ref, payload)
                return "skipped"
            self._event(
                "artifact.local_invalid",
                ref,
                {**self._base_detail(ref), "reason": "existing file failed re-verification"},
                "warning",
            )

        part_limit = None
        if capacity is not None:
            try:
                part_limit = capacity.admit_download(ref)
            except CapacityAdmissionError as exc:
                if exc.replan:
                    return "deferred"
                return self._capacity_blocked(ref, exc.code, exc.detail)

        self.ledger.update_artifact(
            ref.artifact_uid,
            self.run_id,
            status="downloading",
            error_code=None,
            error_detail=None,
            quarantine_path=None,
        )
        self._event("artifact.download_start", ref, self._base_detail(ref))
        result = self.downloader.download(ref, part_limit=part_limit)
        if not result.accepted or result.path is None:
            code = result.error_code or "download_failed"
            detail = result.error_detail or "download did not complete"
            if code == "capacity_exceeded":
                return self._capacity_blocked(ref, code, detail)
            self.ledger.update_artifact(
                ref.artifact_uid,
                self.run_id,
                status="failed",
                local_path=None,
                attempts=result.attempts,
                error_code=result.error_code or "download_failed",
                error_detail=result.error_detail or "download did not complete",
            )
            payload = self._base_detail(ref)
            payload.update(
                {
                    "attempts": result.attempts,
                    "code": result.error_code or "download_failed",
                    "detail": result.error_detail or "download did not complete",
                }
            )
            self._event("artifact.failed", ref, payload, "warning")
            return "failed"

        try:
            integrity = verify_file(result.path, ref.expected_size, ref.expected_sha256)
        except (IntegrityError, OSError) as exc:
            self._fail(ref, "verification_error", str(exc))
            return "failed"
        if integrity.accepted:
            local_path = relative_posix(self.config.root, result.path)
            self.ledger.update_artifact(
                ref.artifact_uid,
                self.run_id,
                status="verified",
                local_path=local_path,
                actual_size=integrity.actual_size,
                actual_sha256=integrity.actual_sha256,
                attempts=result.attempts,
                error_code=None,
                error_detail=None,
                quarantine_path=None,
            )
            payload = self._base_detail(ref)
            payload.update(
                {
                    "local_path": local_path,
                    "size": integrity.actual_size,
                    "sha256": integrity.actual_sha256,
                    "attempts": result.attempts,
                    "resumed_from": result.resumed_from,
                }
            )
            self._event("artifact.verified", ref, payload)
            return "verified"

        quarantine_path = quarantine_file(
            result.path,
            self.config.download.quarantine_dir,
            ref.dataset_id,
            ref.version_id,
            ref.filename,
        )
        status = "checksum_mismatch" if not integrity.digest_ok else "size_mismatch"
        code = "checksum_mismatch" if not integrity.digest_ok else "size_mismatch"
        detail = (
            f"expected sha256 {ref.expected_sha256}, got {integrity.actual_sha256}"
            if not integrity.digest_ok
            else f"expected size {ref.expected_size}, got {integrity.actual_size}"
        )
        self.ledger.update_artifact(
            ref.artifact_uid,
            self.run_id,
            status=status,
            local_path=None,
            quarantine_path=relative_posix(self.config.root, quarantine_path),
            actual_size=integrity.actual_size,
            actual_sha256=integrity.actual_sha256,
            attempts=result.attempts,
            error_code=code,
            error_detail=detail,
        )
        payload = self._base_detail(ref)
        payload.update(
            {
                "expected_sha256": ref.expected_sha256,
                "actual_sha256": integrity.actual_sha256,
                "expected_size": ref.expected_size,
                "actual_size": integrity.actual_size,
                "quarantine_path": relative_posix(self.config.root, quarantine_path),
            }
        )
        self._event("artifact.quarantined", ref, payload, "warning")
        return status
