"""Deterministic JSON, Markdown, CSV, and JSONL audit output."""

from __future__ import annotations

import csv
import io
import json
import sqlite3

from .budget import budget_markdown_lines, budget_report_section
from .util import atomic_write_text, ensure_directory, relative_posix


def _event_rows(connection: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = connection.execute(
        "SELECT seq, ts, level, code, subject, detail_json FROM events "
        "WHERE run_id = ? ORDER BY seq",
        (run_id,),
    ).fetchall()
    events = []
    for row in rows:
        try:
            detail = json.loads(row["detail_json"])
        except json.JSONDecodeError:
            detail = {}
        events.append(
            {
                "seq": row["seq"],
                "ts": row["ts"],
                "level": row["level"],
                "code": row["code"],
                "subject": row["subject"],
                "detail": detail,
            }
        )
    return events


def collect_counts(connection: sqlite3.Connection, run_id: str) -> dict:
    datasets = connection.execute(
        "SELECT COUNT(*) FROM datasets WHERE last_run_id = ?", (run_id,)
    ).fetchone()[0]
    versions = connection.execute(
        "SELECT COUNT(*) FROM versions WHERE last_run_id = ?", (run_id,)
    ).fetchone()[0]
    rows = connection.execute(
        "SELECT status, COUNT(*) AS amount FROM artifacts WHERE last_run_id = ? GROUP BY status",
        (run_id,),
    ).fetchall()
    statuses = {row["status"]: row["amount"] for row in rows}
    events = _event_rows(connection, run_id)
    return {
        "datasets": datasets,
        "versions": versions,
        "artifacts": sum(statuses.values()),
        "verified": statuses.get("verified", 0),
        "skipped": statuses.get("skipped", 0),
        "quarantined": statuses.get("checksum_mismatch", 0)
        + statuses.get("size_mismatch", 0),
        "failed": statuses.get("failed", 0),
        "pending": statuses.get("pending", 0) + statuses.get("downloading", 0),
        "blocked_targets": sum(1 for item in events if item["code"] == "target.blocked"),
        "warnings": sum(1 for item in events if item["level"] == "warning"),
        "events": len(events),
    }


def _artifact_dict(row: sqlite3.Row) -> dict:
    return {
        "uid": row["artifact_uid"],
        "filename": row["filename"],
        "media_type": row["media_type"],
        "url": row["download_url"],
        "status": row["status"],
        "expected": {"size": row["expected_size"], "sha256": row["expected_sha256"]},
        "observed": {
            "size": row["actual_size"],
            "sha256": row["actual_sha256"],
            "remote_size": row["remote_size"],
            "remote_media_type": row["remote_media_type"],
            "accept_ranges": row["accept_ranges"],
        },
        "local_path": row["local_path"],
        "quarantine_path": row["quarantine_path"],
        "attempts": row["attempts"],
        "error": (
            None
            if row["error_code"] is None
            else {"code": row["error_code"], "detail": row["error_detail"]}
        ),
    }


def build_report(connection: sqlite3.Connection, run_id: str) -> dict:
    run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(run_id)
    datasets = connection.execute(
        "SELECT * FROM datasets WHERE last_run_id = ? ORDER BY dataset_id", (run_id,)
    ).fetchall()
    versions = connection.execute(
        "SELECT * FROM versions WHERE last_run_id = ? ORDER BY dataset_id, version_id",
        (run_id,),
    ).fetchall()
    artifacts = connection.execute(
        "SELECT * FROM artifacts WHERE last_run_id = ? "
        "ORDER BY dataset_id, version_id, filename",
        (run_id,),
    ).fetchall()
    events = _event_rows(connection, run_id)
    summary = collect_counts(connection, run_id)
    try:
        stored_summary = json.loads(run["summary_json"]) if run["summary_json"] else {}
    except json.JSONDecodeError:
        stored_summary = {}
    if stored_summary:
        summary = stored_summary
    by_version: dict[tuple[str, str], list[dict]] = {}
    for artifact in artifacts:
        by_version.setdefault((artifact["dataset_id"], artifact["version_id"]), []).append(
            _artifact_dict(artifact)
        )
    by_dataset: dict[str, list[dict]] = {}
    for version in versions:
        by_dataset.setdefault(version["dataset_id"], []).append(
            {
                "id": version["version_id"],
                "label": version["label"],
                "channel": version["channel"],
                "released_at": version["released_at"],
                "source_url": version["source_url"],
                "artifacts": by_version.get(
                    (version["dataset_id"], version["version_id"]), []
                ),
            }
        )
    dataset_items = []
    for dataset in datasets:
        dataset_items.append(
            {
                "id": dataset["dataset_id"],
                "title": dataset["title"],
                "publisher": dataset["publisher"],
                "license": dataset["license"],
                "source_url": dataset["source_url"],
                "versions": by_dataset.get(dataset["dataset_id"], []),
            }
        )
    report = {
        "schema_version": 1,
        "run": {
            "run_id": run["run_id"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "config_fingerprint": run["config_fingerprint"],
        },
        "summary": summary,
        "datasets": dataset_items,
        "events": events,
    }
    adjudications = budget_report_section(events)
    if adjudications:
        report["budget_adjudications"] = adjudications
    return report


def _markdown(report: dict) -> str:
    run = report["run"]
    summary = report["summary"]
    lines = [
        "# ReleaseHarbor release intake report",
        "",
        f"- Run: `{run['run_id']}`",
        f"- Status: **{run['status']}**",
        f"- Started: {run['started_at']}",
        f"- Finished: {run['finished_at']}",
        f"- Datasets / versions / artifacts: {summary['datasets']} / "
        f"{summary['versions']} / {summary['artifacts']}",
        f"- Verified / skipped / quarantined / failed: {summary['verified']} / "
        f"{summary['skipped']} / {summary['quarantined']} / {summary['failed']}",
        f"- Blocked targets: {summary['blocked_targets']}",
        "",
        "> All names, payloads, checksums, and URLs in this report are synthetic fixtures.",
        "",
    ]
    for dataset in report["datasets"]:
        lines.extend(
            [
                f"## {dataset['title']} (`{dataset['id']}`)",
                "",
                f"{dataset['publisher']} | {dataset['license']} | {dataset['source_url']}",
                "",
            ]
        )
        for version in dataset["versions"]:
            lines.extend(
                [
                    f"### {version['label']} (`{version['id']}`, {version['channel']})",
                    "",
                    "| Artifact | Status | Expected bytes | Actual bytes | Attempts |",
                    "|---|---|---:|---:|---:|",
                ]
            )
            for artifact in version["artifacts"]:
                observed = artifact["observed"]["size"]
                lines.append(
                    f"| {artifact['filename']} | {artifact['status']} | "
                    f"{artifact['expected']['size']} | "
                    f"{'' if observed is None else observed} | {artifact['attempts']} |"
                )
            lines.append("")
    adjudications = report.get("budget_adjudications")
    if adjudications:
        lines.extend(budget_markdown_lines(adjudications))
    lines.append("End of deterministic fixture report.\n")
    return "\n".join(lines)


def _manifest_csv(report: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "dataset_id",
            "version_id",
            "filename",
            "media_type",
            "expected_size",
            "expected_sha256",
            "actual_size",
            "actual_sha256",
            "status",
            "local_path",
            "quarantine_path",
            "attempts",
        ]
    )
    for dataset in report["datasets"]:
        for version in dataset["versions"]:
            for artifact in version["artifacts"]:
                writer.writerow(
                    [
                        dataset["id"],
                        version["id"],
                        artifact["filename"],
                        artifact["media_type"],
                        artifact["expected"]["size"],
                        artifact["expected"]["sha256"],
                        "" if artifact["observed"]["size"] is None else artifact["observed"]["size"],
                        artifact["observed"]["sha256"] or "",
                        artifact["status"],
                        artifact["local_path"] or "",
                        artifact["quarantine_path"] or "",
                        artifact["attempts"],
                    ]
                )
    return buffer.getvalue()


def write_reports(connection: sqlite3.Connection, config, run_id: str) -> dict[str, str]:
    report = build_report(connection, run_id)
    directory = config.download.report_dir
    ensure_directory(directory)
    json_path = directory / "report.json"
    markdown_path = directory / "report.md"
    manifest_path = directory / "manifest.csv"
    audit_path = directory / "audit.jsonl"
    atomic_write_text(
        json_path,
        json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
    )
    atomic_write_text(markdown_path, _markdown(report))
    atomic_write_text(manifest_path, _manifest_csv(report))
    audit_lines = [
        json.dumps(event, ensure_ascii=True, sort_keys=True) for event in report["events"]
    ]
    atomic_write_text(audit_path, "\n".join(audit_lines) + ("\n" if audit_lines else ""))
    return {
        "report_json": relative_posix(config.root, json_path),
        "report_markdown": relative_posix(config.root, markdown_path),
        "manifest_csv": relative_posix(config.root, manifest_path),
        "audit_jsonl": relative_posix(config.root, audit_path),
    }
