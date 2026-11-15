"""Read-only loopback review console over the SQLite ledger."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .db import connect_readonly
from .errors import ReleaseHarborError
from .reports import build_report
from .console_pages import (
    artifact_detail_page,
    artifact_list_page,
    dashboard_page,
    error_page,
    run_page,
)
from .security import is_loopback_host

_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_ARTIFACT_UID = re.compile(r"[0-9a-f]{24}\Z")
_STATUS = re.compile(r"[a-z_]{0,40}\Z")
_STATUSES = {"", "pending", "downloading", "verified", "skipped", "failed", "checksum_mismatch", "size_mismatch"}


class _ConsoleHTTPServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, address, handler, db_path: Path) -> None:
        self.db_path = db_path
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ReleaseHarborReview/1.0"
    sys_version = ""

    def log_message(self, format, *args) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send(
            error_page(405, "This console only implements GET and HEAD."),
            405,
            {"Allow": "GET, HEAD"},
        )

    def _send(self, html: str, status: int = 200, extra: dict | None = None) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _host_ok(self) -> bool:
        header = self.headers.get("Host", "")
        if not header:
            return True
        try:
            hostname = urlsplit("//" + header).hostname or ""
        except ValueError:
            return False
        return is_loopback_host(hostname)

    def _dispatch(self) -> None:
        if len(self.path) > 512 or not self._host_ok():
            self._send(error_page(421, "Invalid or non-loopback Host header."), 421)
            return
        connection = None
        try:
            connection = connect_readonly(self.server.db_path)  # type: ignore[attr-defined]
            self._route(connection)
        except ReleaseHarborError:
            self._send(error_page(503, "The local ledger is unavailable or unreadable."), 503)
        except sqlite3.Error:
            self._send(error_page(503, "The local ledger could not be queried."), 503)
        except Exception:
            self._send(error_page(500, "Unexpected read failure."), 500)
        finally:
            if connection is not None:
                connection.close()

    def _route(self, connection: sqlite3.Connection) -> None:
        parts = urlsplit(self.path)
        segments = [unquote(item) for item in parts.path.split("/") if item]
        if not segments:
            self._dashboard(connection)
            return
        if segments == ["artifacts"]:
            self._artifact_list(connection, parts.query)
            return
        if len(segments) == 2 and segments[0] == "runs" and _RUN_ID.fullmatch(segments[1]):
            self._run(connection, segments[1])
            return
        if (
            len(segments) == 2
            and segments[0] == "artifact"
            and _ARTIFACT_UID.fullmatch(segments[1])
        ):
            self._artifact(connection, segments[1])
            return
        self._send(error_page(404, "The requested read-only page does not exist."), 404)

    def _dashboard(self, connection: sqlite3.Connection) -> None:
        runs = []
        for row in connection.execute(
            "SELECT run_id, status, started_at, finished_at, summary_json FROM runs "
            "ORDER BY started_at DESC, run_id DESC LIMIT 20"
        ).fetchall():
            item = dict(row)
            try:
                item["summary"] = json.loads(item.pop("summary_json") or "{}")
            except json.JSONDecodeError:
                item["summary"] = {}
            runs.append(item)
        totals = {
            "datasets": connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0],
            "versions": connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0],
            "artifacts": connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            "runs": connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        }
        status_counts = {
            row["status"]: row["amount"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS amount FROM artifacts GROUP BY status ORDER BY status"
            ).fetchall()
        }
        self._send(dashboard_page(runs, totals, status_counts))

    def _run(self, connection: sqlite3.Connection, run_id: str) -> None:
        try:
            report = build_report(connection, run_id)
        except KeyError:
            self._send(error_page(404, "Run not found."), 404)
            return
        self._send(run_page(report))

    def _artifact_list(self, connection: sqlite3.Connection, query: str) -> None:
        values = parse_qs(query, keep_blank_values=True)
        selected = values.get("status", [""])[0]
        if not _STATUS.fullmatch(selected) or selected not in _STATUSES:
            self._send(error_page(400, "Unknown artifact status filter."), 400)
            return
        sql = (
            "SELECT artifact_uid, dataset_id, version_id, filename, status, expected_size, "
            "attempts FROM artifacts"
        )
        parameters: tuple = ()
        if selected:
            sql += " WHERE status = ?"
            parameters = (selected,)
        sql += " ORDER BY dataset_id, version_id, filename LIMIT 500"
        rows = [dict(row) for row in connection.execute(sql, parameters).fetchall()]
        self._send(artifact_list_page(rows, selected))

    def _artifact(self, connection: sqlite3.Connection, artifact_uid: str) -> None:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_uid = ?", (artifact_uid,)
        ).fetchone()
        if row is None:
            self._send(error_page(404, "Artifact not found."), 404)
            return
        events = [
            dict(event)
            for event in connection.execute(
                "SELECT seq, ts, level, code, detail_json FROM events WHERE subject = ? "
                "ORDER BY event_id",
                (artifact_uid,),
            ).fetchall()
        ]
        self._send(artifact_detail_page(dict(row), events))


class ConsoleServer:
    """Owns the loopback review console and its background thread."""

    def __init__(self, db_path: Path, port: int = 8766) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("console port is invalid")
        self._server = _ConsoleHTTPServer(("127.0.0.1", port), _Handler, db_path)
        self._thread: threading.Thread | None = None
        self.port = self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "ConsoleServer":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="releaseharbor-review-console", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> "ConsoleServer":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()
