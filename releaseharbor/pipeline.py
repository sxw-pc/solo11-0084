"""End-to-end collection orchestration for one auditable run."""

from __future__ import annotations

from dataclasses import dataclass

from .admission import CapacityRunner
from .clock import Clock
from .collector import ArtifactProcessor
from .db import connect
from .discovery import Discovery, DiscoveryError, DiscoveryResult
from .downloader import Downloader
from .errors import ReleaseHarborError
from .httpclient import SafeHttpClient
from .ledger import Ledger
from .reports import collect_counts, write_reports
from .security import UrlPolicy
from .workspace import reset_outputs


@dataclass
class RunOutcome:
    run_id: str
    status: str
    summary: dict
    reports: dict[str, str]
    pages_fetched: int


def _persist_manifest(ledger: Ledger, run_id: str, result: DiscoveryResult) -> int:
    artifact_count = 0
    for dataset in result.datasets:
        ledger.upsert_dataset(run_id, dataset)
        for version in dataset.versions:
            ledger.upsert_version(run_id, version, dataset.dataset_id)
            artifact_count += len(version.artifacts)
    return artifact_count


def run_pipeline(config, *, reset: bool = False) -> RunOutcome:
    if reset:
        reset_outputs(config)
    config.ensure_output_dirs()
    connection = connect(config.download.db_path)
    clock = Clock.from_settings(config.clock_mode, config.clock_fixed)
    ledger = Ledger(connection, clock)
    preferred = f"run-{clock.compact()}-{config.fingerprint[:8]}"
    run_id = ledger.allocate_run_id(preferred)
    ledger.start_run(run_id, config.fingerprint)
    try:
        ledger.record_event(
            run_id,
            "run.started",
            run_id,
            {
                "config_fingerprint": config.fingerprint,
                "portal": config.portal.base_url,
                "clock_mode": config.clock_mode,
            },
        )

        divergence: list[str] = []

        def audit(code: str, subject: str, detail: dict) -> None:
            if code in {"download.range_ignored", "download.partial_discarded"}:
                divergence.append(code)
            level = "warning" if code in {"target.blocked", "discovery.channel_filtered"} else "info"
            ledger.record_event(run_id, code, subject, detail, level)

        def blocked(code: str, subject: str, detail: dict) -> None:
            ledger.record_event(run_id, code, subject, detail, "warning")

        policy = UrlPolicy(config.portal.base_url)
        client = SafeHttpClient(
            policy, timeout=config.request_timeout, on_blocked=blocked
        )
        discovery = Discovery(client, config, on_event=audit)
        result = discovery.discover()
        artifact_count = _persist_manifest(ledger, run_id, result)
        audit(
            "discovery.completed",
            "portal",
            {
                "datasets": len(result.datasets),
                "versions": sum(len(item.versions) for item in result.datasets),
                "artifacts": artifact_count,
                "pages_fetched": result.pages_fetched,
                "blocked": len(result.blocked),
                "issues": len(result.issues),
            },
        )
        if artifact_count == 0:
            raise DiscoveryError("discovery returned no release artifacts")

        downloader = Downloader(config, client, clock, on_event=audit)
        processor = ArtifactProcessor(config, client, ledger, downloader, run_id)
        refs = [
            ref
            for dataset in result.datasets
            for version in dataset.versions
            for ref in version.artifacts
        ]
        if config.capacity is not None:
            runner = CapacityRunner(config, connection, ledger, processor, run_id, divergence)
            runner.execute(refs)
        else:
            for ref in refs:
                try:
                    processor.process(ref)
                except Exception as exc:  # keep one bad artifact from aborting the run
                    detail = {
                        "dataset": ref.dataset_id,
                        "version": ref.version_id,
                        "filename": ref.filename,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    ledger.record_event(
                        run_id, "artifact.failed", ref.artifact_uid, detail, "error"
                    )
                    try:
                        ledger.update_artifact(
                            ref.artifact_uid,
                            run_id,
                            status="failed",
                            local_path=None,
                            error_code="internal_error",
                            error_detail=str(exc),
                        )
                    except Exception:
                        pass

        counts = collect_counts(connection, run_id)
        blocked = counts.get("capacity_blocked", 0) + counts.get("retired", 0)
        status = (
            "partial"
            if counts["failed"] or counts["quarantined"] or counts["pending"] or blocked
            else "success"
        )
        ledger.record_event(run_id, "run.finished", run_id, {"status": status})
        summary = collect_counts(connection, run_id)
        ledger.finish_run(run_id, status, summary)
        paths = write_reports(connection, config, run_id)
        return RunOutcome(run_id, status, summary, paths, result.pages_fetched)
    except Exception as exc:
        detail = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            ledger.record_event(run_id, "run.failed", run_id, detail, "error")
            summary = collect_counts(connection, run_id)
            summary["fatal_error"] = detail["error"]
            ledger.finish_run(run_id, "failed", summary)
            write_reports(connection, config, run_id)
        except Exception:
            pass
        if isinstance(exc, ReleaseHarborError):
            raise
        raise ReleaseHarborError(str(exc)) from exc
    finally:
        connection.close()
