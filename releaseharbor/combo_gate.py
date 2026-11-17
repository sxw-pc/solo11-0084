"""Acceptance gates for combination inputs.

Every listed artifact must already be accepted in the intake ledger, its bytes
must still match on disk, and the request must not repeat a UID. These checks
never change ledger or acceptance state.
"""

from __future__ import annotations

from pathlib import Path

from .combo_models import ACCEPTED_LEDGER_STATUSES, ComboSource, GateFailure
from .errors import IntegrityError
from .verify import verify_file


def gate_sources(
    sources: list[ComboSource], connection, root: Path
) -> tuple[list[dict], list[GateFailure]]:
    failures: list[GateFailure] = []
    evidence: list[dict] = []
    seen: set[str] = set()
    for position, source in enumerate(sources):
        if source.artifact_uid in seen:
            failures.append(
                GateFailure(
                    "duplicate_artifact_uid",
                    source.artifact_uid,
                    f"sources[{position}] repeats an artifact already listed; the "
                    "combination input must list each accepted artifact once",
                )
            )
        seen.add(source.artifact_uid)
        row = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_uid = ?", (source.artifact_uid,)
        ).fetchone()
        if row is None:
            failures.append(
                GateFailure(
                    "artifact_not_in_ledger",
                    source.artifact_uid,
                    f"sources[{position}] is absent from the intake ledger",
                )
            )
            evidence.append(
                {
                    "position": position,
                    "artifact_uid": source.artifact_uid,
                    "fictional_label": source.fictional_label,
                    "expected_sha256": source.expected_sha256,
                    "ledger_status": None,
                }
            )
            continue
        entry = {
            "position": position,
            "artifact_uid": source.artifact_uid,
            "dataset_id": row["dataset_id"],
            "version_id": row["version_id"],
            "filename": row["filename"],
            "fictional_label": source.fictional_label,
            "expected_sha256": source.expected_sha256,
            "ledger_status": row["status"],
            "local_path": row["local_path"],
        }
        if row["status"] not in ACCEPTED_LEDGER_STATUSES:
            failures.append(
                GateFailure(
                    "artifact_not_accepted",
                    source.artifact_uid,
                    f"sources[{position}] ledger status is {row['status']!r}, not one of "
                    f"{sorted(ACCEPTED_LEDGER_STATUSES)}",
                )
            )
        if row["expected_sha256"] != source.expected_sha256:
            failures.append(
                GateFailure(
                    "expected_digest_mismatch",
                    source.artifact_uid,
                    f"sources[{position}] expected_sha256 does not match the accepted ledger "
                    f"digest {row['expected_sha256']}",
                )
            )
        if not row["local_path"]:
            failures.append(
                GateFailure(
                    "accepted_file_missing",
                    source.artifact_uid,
                    f"sources[{position}] has no accepted local file",
                )
            )
        else:
            try:
                report = verify_file(
                    root / row["local_path"], row["expected_size"], source.expected_sha256
                )
                entry["disk_recheck"] = {
                    "size_ok": report.size_ok,
                    "digest_ok": report.digest_ok,
                    "actual_size": report.actual_size,
                    "actual_sha256": report.actual_sha256,
                }
                if not report.accepted:
                    failures.append(
                        GateFailure(
                            "disk_bytes_mismatch",
                            source.artifact_uid,
                            f"sources[{position}] on-disk re-verification failed "
                            f"(size_ok={report.size_ok}, digest_ok={report.digest_ok})",
                        )
                    )
            except (IntegrityError, OSError) as exc:
                entry["disk_recheck"] = {"error": str(exc)}
                failures.append(
                    GateFailure(
                        "disk_bytes_mismatch",
                        source.artifact_uid,
                        f"sources[{position}] cannot re-verify disk bytes: {exc}",
                    )
                )
        evidence.append(entry)
    return evidence, failures
