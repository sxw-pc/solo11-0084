"""Write-side operations for the SQLite release ledger."""

from __future__ import annotations

import json
import sqlite3

from .csvschema import SchemaDeclaration, schema_lineage_key
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

    def record_schema_declaration(self, run_id: str, ref: ArtifactRef) -> sqlite3.Row:
        """Persist an append-only declaration history entry.

        Re-encountering the exact same declaration is idempotent; a changed
        declaration for the same (lineage, schema_version) inserts a new row so
        every prior verdict stays reproducible.
        """
        declaration = ref.schema
        assert declaration is not None
        lineage = schema_lineage_key(ref.dataset_id, ref.filename)
        uid = stable_id(lineage, declaration.schema_version, declaration.fingerprint())
        now = self.clock.now()
        self.connection.execute(
            "INSERT INTO csv_schema_declarations(declaration_uid, lineage_key, dataset_id, "
            "filename, schema_version, predecessor, change_reason, fingerprint, "
            "declaration_json, first_seen_run_id, last_run_id, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(lineage_key, schema_version, fingerprint) DO UPDATE SET "
            "last_run_id = excluded.last_run_id, updated_at = excluded.updated_at",
            (
                uid,
                lineage,
                ref.dataset_id,
                ref.filename,
                declaration.schema_version,
                declaration.predecessor,
                declaration.change_reason,
                declaration.fingerprint(),
                canonical_json(declaration.to_dict()),
                run_id,
                run_id,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.get_schema_declaration(uid)

    def get_schema_declaration(self, declaration_uid: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM csv_schema_declarations WHERE declaration_uid = ?",
            (declaration_uid,),
        ).fetchone()
        if row is None:
            raise KeyError(declaration_uid)
        return row

    def lineage_declarations(self, lineage_key: str) -> list[SchemaDeclaration]:
        rows = self.connection.execute(
            "SELECT declaration_json FROM csv_schema_declarations "
            "WHERE lineage_key = ? ORDER BY discovered_at, declaration_uid",
            (lineage_key,),
        ).fetchall()
        declarations = []
        for row in rows:
            try:
                declarations.append(SchemaDeclaration.from_dict(json.loads(row["declaration_json"])))
            except (json.JSONDecodeError, ValueError):
                continue
        return declarations

    def find_predecessor_declaration(
        self, lineage_key: str, predecessor_version: str
    ) -> SchemaDeclaration | None:
        rows = self.connection.execute(
            "SELECT declaration_json FROM csv_schema_declarations "
            "WHERE lineage_key = ? AND schema_version = ? "
            "ORDER BY discovered_at DESC, declaration_uid DESC",
            (lineage_key, predecessor_version),
        ).fetchall()
        for row in rows:
            try:
                return SchemaDeclaration.from_dict(json.loads(row["declaration_json"]))
            except (json.JSONDecodeError, ValueError):
                continue
        return None

    def save_schema_evaluation(self, run_id: str, ref: ArtifactRef, evaluation: dict) -> str:
        """Persist one immutable per-run schema evaluation (idempotent on rerun)."""
        uid = stable_id(run_id, ref.artifact_uid)
        self.connection.execute(
            "INSERT OR IGNORE INTO csv_schema_evaluations(evaluation_uid, run_id, artifact_uid, "
            "lineage_key, dataset_id, version_id, filename, content_sha256, content_size, "
            "schema_version, declaration_fingerprint, declaration_summary_json, "
            "predecessor_version, predecessor_fingerprint, predecessor_summary_json, "
            "change_reason, validation_ok, validation_json, reference_error, "
            "positional_compatible, name_bound_compatible, contracts_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                run_id,
                ref.artifact_uid,
                evaluation["lineage_key"],
                ref.dataset_id,
                ref.version_id,
                ref.filename,
                evaluation["content_sha256"],
                evaluation["content_size"],
                evaluation["schema_version"],
                evaluation["declaration_fingerprint"],
                canonical_json(evaluation["declaration_summary"]),
                evaluation["predecessor_version"],
                evaluation["predecessor_fingerprint"],
                canonical_json(evaluation["predecessor_summary"])
                if evaluation["predecessor_summary"] is not None
                else None,
                evaluation["change_reason"],
                1 if evaluation["validation_ok"] else 0,
                canonical_json(evaluation["validation"]),
                evaluation["reference_error"],
                None
                if evaluation["positional_compatible"] is None
                else int(evaluation["positional_compatible"]),
                None
                if evaluation["name_bound_compatible"] is None
                else int(evaluation["name_bound_compatible"]),
                canonical_json(evaluation["contracts"]),
                self.clock.now(),
            ),
        )
        self.connection.commit()
        return uid

    def get_schema_evaluation(self, run_id: str, artifact_uid: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM csv_schema_evaluations WHERE run_id = ? AND artifact_uid = ?",
            (run_id, artifact_uid),
        ).fetchone()
