"""Server-side HTML rendering for the read-only review console."""

from __future__ import annotations

from html import escape
from urllib.parse import quote

_STYLE = """
:root{color-scheme:light}body{font-family:system-ui,sans-serif;max-width:1180px;margin:0 auto;padding:1.2rem;color:#17211b;background:#f7f8f3}
a{color:#1d5b3a}header,footer{border-bottom:1px solid #aab7ad;padding:.7rem 0}footer{border-top:1px solid #aab7ad;border-bottom:0;margin-top:2rem}
h1,h2,h3{color:#204b34}nav a{margin-right:1rem}.muted{color:#52645a}.tiles{display:flex;gap:.8rem;flex-wrap:wrap}
.tile{border:1px solid #b8c6bc;border-left:5px solid #376c4e;border-radius:6px;padding:.7rem 1rem;background:#fff;min-width:8rem}
.tile strong{display:block;font-size:1.5rem}.panel{border:1px solid #b8c6bc;border-radius:8px;padding:1rem;margin:1rem 0;background:#fff}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #d5ddd6;padding:.4rem;text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}code,pre{background:#edf1ea;border-radius:3px}code{padding:.1rem .25rem;overflow-wrap:anywhere}
pre{padding:.7rem;overflow:auto}.status-verified{color:#11643b;font-weight:600}.status-skipped{color:#315e77}
.status-failed,.status-checksum_mismatch,.status-size_mismatch{color:#9a281e;font-weight:600}.warning{background:#fff5dc}
form{display:flex;gap:.5rem;align-items:center;margin:.8rem 0}select,button{padding:.3rem}
""".strip()


def _e(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def layout(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)} - ReleaseHarbor Review</title><style>{_STYLE}</style></head>"
        "<body><header><strong>ReleaseHarbor Review</strong> "
        '<span class="muted">read-only local ledger</span>'
        '<nav><a href="/">Overview</a><a href="/artifacts">Artifacts</a></nav></header>'
        f"{body}<footer>Loopback only. SQLite opened with mode=ro and query_only.</footer>"
        "</body></html>\n"
    )


def _tile(label: str, value) -> str:
    return f'<div class="tile"><span class="muted">{_e(label)}</span><strong>{_e(value)}</strong></div>'


def dashboard_page(runs: list[dict], totals: dict, status_counts: dict) -> str:
    tiles = "".join(
        [
            _tile("Datasets", totals["datasets"]),
            _tile("Versions", totals["versions"]),
            _tile("Artifacts", totals["artifacts"]),
            _tile("Runs", totals["runs"]),
        ]
    )
    run_rows = []
    for run in runs:
        run_rows.append(
            "<tr>"
            f'<td><a href="/runs/{quote(run["run_id"])}"><code>{_e(run["run_id"])}</code></a></td>'
            f'<td class="status-{_e(run["status"])}">{_e(run["status"])}</td>'
            f'<td>{_e(run["started_at"])}</td><td>{_e(run["finished_at"] or "-")}</td></tr>'
        )
    status_rows = "".join(
        f'<tr><td><a href="/artifacts?status={quote(name)}">{_e(name)}</a></td>'
        f"<td>{_e(amount)}</td></tr>"
        for name, amount in sorted(status_counts.items())
    )
    latest = runs[0] if runs else None
    latest_summary = latest.get("summary") if latest else {}
    latest_panel = ""
    if latest_summary:
        latest_panel = (
            '<section class="panel"><h2>Latest run summary</h2><p>'
            f'<a href="/runs/{quote(latest["run_id"])}"><code>{_e(latest["run_id"])}</code></a>'
            f' status <strong>{_e(latest["status"])}</strong></p><div class="tiles">'
            + "".join(
                _tile(key, latest_summary.get(key, 0))
                for key in ("verified", "skipped", "quarantined", "failed", "blocked_targets")
            )
            + "</div></section>"
        )
    body = (
        '<main><h1>Release intake overview</h1><div class="tiles">'
        + tiles
        + '</div><section class="panel"><h2>Artifact states</h2><table><tbody>'
        + (status_rows or '<tr><td class="muted">No artifacts recorded yet.</td></tr>')
        + "</tbody></table></section>"
        + latest_panel
        + '<section class="panel"><h2>Recent runs</h2><table><thead><tr><th>Run</th>'
        "<th>Status</th><th>Started</th><th>Finished</th></tr></thead><tbody>"
        + "".join(run_rows)
        + "</tbody></table></section></main>"
    )
    return layout("Overview", body)


def run_page(report: dict) -> str:
    run = report["run"]
    summary = report["summary"]
    versions = []
    artifact_rows = []
    for dataset in report["datasets"]:
        for version in dataset["versions"]:
            versions.append(
                f"<li><strong>{_e(version['label'])}</strong> "
                f"<code>{_e(dataset['id'])}/{_e(version['id'])}</code> "
                f"({_e(version['channel'])}, {_e(version['released_at'])})</li>"
            )
            for artifact in version["artifacts"]:
                artifact_rows.append(
                    "<tr>"
                    f'<td><a href="/artifact/{quote(artifact["uid"])}">'
                    f"{_e(artifact['filename'])}</a></td>"
                    f"<td>{_e(dataset['id'])}</td><td>{_e(version['id'])}</td>"
                    f'<td class="status-{_e(artifact["status"])}">{_e(artifact["status"])}</td>'
                    f"<td>{_e(artifact['expected']['size'])}</td>"
                    f"<td>{_e(artifact['attempts'])}</td>"
                    f"<td>{_e(artifact['quarantine_path'] or '')}</td></tr>"
                )
    body = (
        f'<main><p><a href="/">Overview</a></p><h1>Run <code>{_e(run["run_id"])}</code></h1>'
        f'<p>Status <strong>{_e(run["status"])}</strong> | started {_e(run["started_at"])}'
        f' | finished {_e(run["finished_at"] or "-")}</p><div class="tiles">'
        + "".join(
            _tile(key, summary.get(key, 0))
            for key in ("datasets", "versions", "artifacts", "verified", "skipped", "quarantined", "failed")
        )
        + '</div><section class="panel"><h2>Versions</h2><ul>'
        + ("".join(versions) or "<li>None</li>")
        + '</ul></section><section class="panel"><h2>Artifacts</h2><table><thead><tr>'
        "<th>Filename</th><th>Dataset</th><th>Version</th><th>Status</th><th>Bytes</th>"
        "<th>Attempts</th><th>Quarantine</th></tr></thead><tbody>"
        + ("".join(artifact_rows) or '<tr><td class="muted">None</td></tr>')
        + "</tbody></table></section></main>"
    )
    return layout(f"Run {run['run_id']}", body)


def artifact_list_page(rows: list[dict], selected_status: str) -> str:
    options = ["", "verified", "skipped", "failed", "checksum_mismatch", "size_mismatch"]
    controls = "".join(
        f'<option value="{_e(item)}"{" selected" if item == selected_status else ""}>'
        f"{_e(item or 'all states')}</option>"
        for item in options
    )
    table_rows = []
    for row in rows:
        table_rows.append(
            "<tr>"
            f'<td><a href="/artifact/{quote(row["artifact_uid"])}">'
            f"<code>{_e(row['artifact_uid'][:10])}</code></a></td>"
            f"<td>{_e(row['dataset_id'])}</td><td>{_e(row['version_id'])}</td>"
            f"<td>{_e(row['filename'])}</td>"
            f'<td class="status-{_e(row["status"])}">{_e(row["status"])}</td>'
            f"<td>{_e(row['expected_size'])}</td><td>{_e(row['attempts'])}</td></tr>"
        )
    body = (
        '<main><h1>Release artifacts</h1><form method="get" action="/artifacts">'
        f'<label for="status">Status</label><select id="status" name="status">{controls}</select>'
        '<button type="submit">Filter</button></form><p class="muted">Showing at most 500 rows.</p>'
        "<table><thead><tr><th>UID</th><th>Dataset</th><th>Version</th><th>Filename</th>"
        "<th>Status</th><th>Expected bytes</th><th>Attempts</th></tr></thead><tbody>"
        + ("".join(table_rows) or '<tr><td colspan="7" class="muted">No matching artifacts.</td></tr>')
        + "</tbody></table></main>"
    )
    return layout("Artifacts", body)


def artifact_detail_page(row: dict, events: list[dict]) -> str:
    event_rows = "".join(
        "<tr>"
        f"<td>{_e(event['seq'])}</td><td>{_e(event['ts'])}</td>"
        f"<td>{_e(event['level'])}</td><td>{_e(event['code'])}</td>"
        f"<td><code>{_e(event['detail_json'])}</code></td></tr>"
        for event in events
    )
    fields = [
        ("Artifact UID", row["artifact_uid"]),
        ("Dataset", row["dataset_id"]),
        ("Version", row["version_id"]),
        ("Filename", row["filename"]),
        ("Status", row["status"]),
        ("Media type", row["media_type"]),
        ("Download URL", row["download_url"]),
        ("Expected size", row["expected_size"]),
        ("Remote size", row["remote_size"]),
        ("Actual size", row["actual_size"]),
        ("Expected SHA-256", row["expected_sha256"]),
        ("Actual SHA-256", row["actual_sha256"]),
        ("Local path", row["local_path"]),
        ("Quarantine path", row["quarantine_path"]),
        ("Attempts", row["attempts"]),
        ("Error code", row["error_code"]),
        ("Error detail", row["error_detail"]),
        ("Last run", row["last_run_id"]),
    ]
    definition_rows = "".join(
        f"<tr><th>{_e(label)}</th><td><code>{_e(value)}</code></td></tr>"
        for label, value in fields
    )
    error_panel = (
        f'<section class="panel warning"><h2>Failure detail</h2><pre>{_e(row["error_detail"])}</pre></section>'
        if row["error_detail"]
        else ""
    )
    body = (
        f'<main><p><a href="/artifacts">All artifacts</a></p><h1>{_e(row["filename"])}</h1>'
        f'<p>Status <strong class="status-{_e(row["status"])}">{_e(row["status"])}</strong></p>'
        f"<section class=\"panel\"><table><tbody>{definition_rows}</tbody></table></section>"
        + error_panel
        + '<section class="panel"><h2>Audit events</h2><table><thead><tr><th>Seq</th>'
        "<th>Time</th><th>Level</th><th>Code</th><th>Detail</th></tr></thead><tbody>"
        + (event_rows or '<tr><td colspan="5" class="muted">No events.</td></tr>')
        + "</tbody></table></section></main>"
    )
    return layout(row["filename"], body)


def error_page(status: int, message: str) -> str:
    return layout(
        f"Error {status}",
        f'<main><h1>{_e(status)}</h1><p>{_e(message)}</p><p><a href="/">Overview</a></p></main>',
    )
