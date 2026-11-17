"""Per-artifact metadata collection, transfer, and integrity acceptance."""

from __future__ import annotations

from .discovery import ArtifactRef
from .downloader import Downloader, artifact_destination
from .errors import IntegrityError, SecurityBoundaryError, TransportError
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

    def process(self, ref: ArtifactRef) -> str:
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

        self.ledger.update_artifact(
            ref.artifact_uid,
            self.run_id,
            status="downloading",
            error_code=None,
            error_detail=None,
            quarantine_path=None,
        )
        self._event("artifact.download_start", ref, self._base_detail(ref))
        result = self.downloader.download(ref)
        if not result.accepted or result.path is None:
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


# The offline observation-mirror contracts are scoped to the synthetic
# air-quality release and its two documented media types.  Restricting scope
# this way keeps unrelated CSV-only datasets out of the observation reports.
SYNTHETIC_OBSERVATION_DATASET = "air-quality"
OBSERVATION_MEDIA_TYPES = ("text/csv", "application/x-ndjson")


def observation_sources(config, connection, run_id: str) -> dict[str, dict[str, object]]:
    """Re-read observation artifacts for offline dual-format adjudication.

    Scope is the synthetic ``air-quality`` dataset restricted to the two
    documented observation media types.  Once a version is in scope, both
    sides are loaded even when one is missing, quarantined, or no longer
    matches its accepted digest, so the contracts can answer ``unknown``
    instead of silently omitting the version.

    This runs after the ordinary intake loop and never changes an artifact's
    download, acceptance, or quarantine outcome.
    """

    from .observations import ArtifactSource, Issue, load_artifact_source

    rows = connection.execute(
        "SELECT * FROM artifacts WHERE last_run_id = ? ORDER BY dataset_id, version_id, filename",
        (run_id,),
    ).fetchall()
    by_version: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        by_version.setdefault((row["dataset_id"], row["version_id"]), []).append(dict(row))

    grouped: dict[str, dict[str, object]] = {}
    for (dataset_id, version_id), artifacts in sorted(by_version.items()):
        if dataset_id != SYNTHETIC_OBSERVATION_DATASET:
            continue
        if not any(
            (item.get("media_type") or "") in OBSERVATION_MEDIA_TYPES
            for item in artifacts
        ):
            continue
        csv_row = next(
            (item for item in artifacts if (item.get("media_type") or "") == "text/csv"),
            None,
        )
        json_row = next(
            (
                item
                for item in artifacts
                if (item.get("media_type") or "") == "application/x-ndjson"
            ),
            None,
        )
        sources: dict[str, ArtifactSource] = {}
        for kind, row, media, fallback in (
            ("csv", csv_row, "text/csv", "samples.csv"),
            ("jsonl", json_row, "application/x-ndjson", "samples.jsonl"),
        ):
            filename = (row or {}).get("filename") or fallback
            if row is None:
                sources[kind] = ArtifactSource(
                    kind=kind,
                    filename=filename,
                    media_type=media,
                    status="absent",
                    expected_size=0,
                    expected_sha256="",
                    issues=[
                        Issue(
                            kind,
                            None,
                            "artifact_missing",
                            {"filename": filename, "reason": "no counterpart in this version"},
                        )
                    ],
                )
                continue
            local_path = None
            raw_local = row.get("local_path") or row.get("quarantine_path")
            if raw_local:
                candidate = (config.root / raw_local).resolve()
                try:
                    candidate.relative_to(config.root.resolve())
                except ValueError:
                    candidate = None
                local_path = candidate
            sources[kind] = load_artifact_source(
                kind=kind,
                filename=filename,
                media_type=row.get("media_type") or media,
                status=row.get("status") or "failed",
                expected_size=int(row.get("expected_size") or 0),
                expected_sha256=row.get("expected_sha256") or "",
                path=local_path,
            )
        grouped.setdefault(dataset_id, {})[version_id] = sources
    return grouped
