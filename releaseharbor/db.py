"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import ReleaseHarborError
from .util import ensure_directory

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    license TEXT NOT NULL,
    source_url TEXT NOT NULL,
    first_seen_run_id TEXT NOT NULL,
    last_run_id TEXT NOT NULL,
    discovered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    version_uid TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    version_id TEXT NOT NULL,
    label TEXT NOT NULL,
    channel TEXT NOT NULL,
    released_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    first_seen_run_id TEXT NOT NULL,
    last_run_id TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    UNIQUE(dataset_id, version_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_uid TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    download_url TEXT NOT NULL,
    media_type TEXT NOT NULL,
    expected_size INTEGER NOT NULL,
    expected_sha256 TEXT NOT NULL,
    remote_size INTEGER,
    remote_media_type TEXT,
    remote_etag TEXT,
    accept_ranges TEXT,
    status TEXT NOT NULL,
    local_path TEXT,
    quarantine_path TEXT,
    actual_size INTEGER,
    actual_sha256 TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_detail TEXT,
    first_seen_run_id TEXT NOT NULL,
    last_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dataset_id, version_id, filename),
    FOREIGN KEY(dataset_id, version_id) REFERENCES versions(dataset_id, version_id)
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE(run_id, seq)
);
CREATE TABLE IF NOT EXISTS page_evidence (
    run_id TEXT NOT NULL,
    page_url TEXT NOT NULL,
    level TEXT NOT NULL,
    parent_url TEXT,
    fetch_seq INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    links_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, page_url, fetch_seq)
);
CREATE TABLE IF NOT EXISTS coverage_declarations (
    run_id TEXT NOT NULL,
    level TEXT NOT NULL,
    subject TEXT NOT NULL,
    page_url TEXT NOT NULL,
    parent_url TEXT,
    content_digest TEXT NOT NULL,
    first_fetch_seq INTEGER NOT NULL,
    verified_fetch_seq INTEGER,
    fetched_at TEXT NOT NULL,
    stability TEXT NOT NULL,
    complete INTEGER NOT NULL,
    candidates INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    decisions_json TEXT NOT NULL,
    children_json TEXT NOT NULL,
    PRIMARY KEY (run_id, page_url)
);
CREATE TABLE IF NOT EXISTS catalog_views (
    contract TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    view_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (contract, run_id)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(status);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_versions_dataset ON versions(dataset_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON page_evidence(run_id, page_url);
CREATE INDEX IF NOT EXISTS idx_declarations_run ON coverage_declarations(run_id, level);
""".strip()


def connect(db_path: Path) -> sqlite3.Connection:
    ensure_directory(db_path.parent)
    connection = sqlite3.connect(db_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA)
    row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row["value"]) != SCHEMA_VERSION:
        raise ReleaseHarborError(
            f"database schema {row['value']} is not supported (expected {SCHEMA_VERSION})"
        )
    connection.commit()


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise ReleaseHarborError(f"database does not exist: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None or int(row["value"]) != SCHEMA_VERSION:
        connection.close()
        raise ReleaseHarborError("database schema version is missing or unsupported")
    return connection


def database_exists(db_path: Path) -> bool:
    return db_path.is_file()
