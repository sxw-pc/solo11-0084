"""Write-side operations for the SQLite release ledger."""

from __future__ import annotations

import json
import sqlite3

from .discovery import ArtifactRef, DiscoveredDataset, DiscoveredVersion
from .util import canonical_json, stable_id

_ARTIFACT_FIELDS = frozenset(
    {
        "remote_size",
        "remote_media_type",
        "remote_etag",
        "accept_ranges",
        "status",
        "local_path",
        "quarantine_path",
        "actual_size",
        "actual_sha256",
        "attempts",
        "error_code",
        "error_detail",
    }
)


class Ledger:
    def __init__(self, connection: sqlite3.Connection, clock) -> None:
        self.connection = connection
        self.clock = clock

    def allocate_run_id(self, preferred: str) -> str:
        candidate = preferred
        suffix = 2
        while self.connection.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (candidate,)
        ).fetchone():
            candidate = f"{preferred}-{suffix:02d}"
            suffix += 1
        return candidate

    def start_run(self, run_id: str, fingerprint: str) -> None:
        self.connection.execute(
            "INSERT INTO runs(run_id, started_at, status, config_fingerprint) "
            "VALUES (?, ?, 'running', ?)",
            (run_id, self.clock.now(), fingerprint),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, status: str, summary: dict) -> None:
        self.connection.execute(
            "UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE run_id = ?",
            (self.clock.now(), status, canonical_json(summary), run_id),
        )
        self.connection.commit()

    def record_event(
        self, run_id: str, code: str, subject: str, detail: dict | None = None, level: str = "info"
    ) -> None:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        self.connection.execute(
            "INSERT INTO events(run_id, seq, ts, level, code, subject, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                int(row["next"]),
                self.clock.now(),
                level,
                code,
                subject,
                canonical_json(detail or {}),
            ),
        )
        self.connection.commit()

    def upsert_dataset(self, run_id: str, dataset: DiscoveredDataset) -> None:
        now = self.clock.now()
        self.connection.execute(
            "INSERT INTO datasets(dataset_id, title, publisher, license, source_url, "
            "first_seen_run_id, last_run_id, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dataset_id) DO UPDATE SET title=excluded.title, "
            "publisher=excluded.publisher, license=excluded.license, "
            "source_url=excluded.source_url, last_run_id=excluded.last_run_id",
            (
                dataset.dataset_id,
                dataset.title,
                dataset.publisher,
                dataset.license,
                dataset.source_url,
                run_id,
                run_id,
                now,
            ),
        )
        self.connection.commit()

    def upsert_version(self, run_id: str, version: DiscoveredVersion, dataset_id: str) -> None:
        now = self.clock.now()
        uid = stable_id(dataset_id, version.version_id)
        self.connection.execute(
            "INSERT INTO versions(version_uid, dataset_id, version_id, label, channel, "
            "released_at, source_url, first_seen_run_id, last_run_id, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(version_uid) DO UPDATE SET label=excluded.label, "
            "channel=excluded.channel, released_at=excluded.released_at, "
            "source_url=excluded.source_url, last_run_id=excluded.last_run_id",
            (
                uid,
                dataset_id,
                version.version_id,
                version.label,
                version.channel,
                version.released_at,
                version.source_url,
                run_id,
                run_id,
                now,
            ),
        )
        self.connection.commit()

    def upsert_artifact(self, run_id: str, ref: ArtifactRef) -> sqlite3.Row:
        now = self.clock.now()
        self.connection.execute(
            "INSERT INTO artifacts(artifact_uid, dataset_id, version_id, filename, download_url, "
            "media_type, expected_size, expected_sha256, status, first_seen_run_id, last_run_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?) "
            "ON CONFLICT(artifact_uid) DO UPDATE SET download_url=excluded.download_url, "
            "media_type=excluded.media_type, expected_size=excluded.expected_size, "
            "expected_sha256=excluded.expected_sha256, last_run_id=excluded.last_run_id, "
            "updated_at=excluded.updated_at",
            (
                ref.artifact_uid,
                ref.dataset_id,
                ref.version_id,
                ref.filename,
                ref.url,
                ref.media_type,
                ref.expected_size,
                ref.expected_sha256,
                run_id,
                run_id,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.get_artifact(ref.artifact_uid)

    def get_artifact(self, artifact_uid: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE artifact_uid = ?", (artifact_uid,)
        ).fetchone()
        if row is None:
            raise KeyError(artifact_uid)
        return row

    def update_artifact(self, artifact_uid: str, run_id: str, **fields) -> None:
        unknown = set(fields) - _ARTIFACT_FIELDS
        if unknown:
            raise ValueError(f"unknown artifact fields: {', '.join(sorted(unknown))}")
        assignments = [f"{name} = ?" for name in fields]
        values = list(fields.values())
        assignments.extend(["last_run_id = ?", "updated_at = ?"])
        values.extend([run_id, self.clock.now(), artifact_uid])
        self.connection.execute(
            f"UPDATE artifacts SET {', '.join(assignments)} WHERE artifact_uid = ?",
            values,
        )
        self.connection.commit()

    def artifact_events(self, run_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT seq, ts, level, code, subject, detail_json FROM events "
            "WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                detail = json.loads(row["detail_json"])
            except json.JSONDecodeError:
                detail = {}
            result.append(
                {
                    "seq": row["seq"],
                    "ts": row["ts"],
                    "level": row["level"],
                    "code": row["code"],
                    "subject": row["subject"],
                    "detail": detail,
                }
            )
        return result
