"""Command-line entry points: demo, collect, and serve."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .clock import Clock
from .combo_service import run_combo_query
from .config import load_config
from .console import ConsoleServer
from .db import connect, database_exists
from .errors import ReleaseHarborError
from .fixture import load_portal_fixture
from .pipeline import run_pipeline
from .portal_server import PortalServer
from .util import load_json_file, relative_posix


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m releaseharbor",
        description="Local release artifact discovery, download, and acceptance ledger.",
    )
    parser.add_argument("--version", action="version", version=f"releaseharbor {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="run the complete offline fixture demonstration")
    demo.add_argument("--config", default="config/demo.json")
    demo.add_argument("--keep", action="store_true", help="do not reset generated outputs")
    demo.add_argument(
        "--strict", action="store_true", help="return non-zero when acceptance is partial"
    )
    collect = subparsers.add_parser("collect", help="collect from an already running portal")
    collect.add_argument("--config", default="config/demo.json")
    collect.add_argument("--reset", action="store_true", help="reset generated outputs first")
    serve = subparsers.add_parser("serve", help="start the read-only loopback review console")
    serve.add_argument("--config", default="config/demo.json")
    serve.add_argument("--port", type=int, default=None)
    combo = subparsers.add_parser(
        "combo",
        help="offline fictional combination adjudication from a local request file",
    )
    combo.add_argument("--config", default="config/demo.json")
    combo.add_argument("request", help="path to a local combination request JSON file")
    return parser


def _print_outcome(outcome, config) -> None:
    summary = outcome.summary
    print(f"run: {outcome.run_id}")
    print(f"status: {outcome.status}")
    print(
        "datasets={datasets} versions={versions} artifacts={artifacts} "
        "verified={verified} skipped={skipped} quarantined={quarantined} "
        "failed={failed} blocked={blocked_targets}".format(**summary)
    )
    for key in ("report_json", "report_markdown", "manifest_csv", "audit_jsonl"):
        print(f"{key}: {outcome.reports[key]}")
    print(f"database: {relative_posix(config.root, config.download.db_path)}")


def _demo(args) -> int:
    config = load_config(args.config)
    fixture = load_portal_fixture(config.portal.fixture)
    parsed = urlsplit(config.portal.base_url)
    port = parsed.port or 80
    portal = PortalServer(fixture, config.portal.bind_host, port)
    try:
        portal.start()
        print(f"fixture portal: {portal.base_url} (loopback, in-process)")
        reset = config.reset_on_start and not args.keep
        outcome = run_pipeline(config, reset=reset)
    finally:
        portal.stop()
        print("fixture portal stopped")
    _print_outcome(outcome, config)
    if args.strict and outcome.status != "success":
        return 1
    return 0


def _collect(args) -> int:
    config = load_config(args.config)
    outcome = run_pipeline(config, reset=args.reset)
    _print_outcome(outcome, config)
    return 0


def _serve(args) -> int:
    config = load_config(args.config)
    if not database_exists(config.download.db_path):
        raise ReleaseHarborError(
            "ledger database does not exist; run the demo or collect command first"
        )
    port = config.console.port if args.port is None else args.port
    server = ConsoleServer(config.download.db_path, port)
    print(f"review console: {server.base_url} (read-only, loopback)")
    print("press Ctrl-C to stop")
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("review console stopped")
    finally:
        server.stop()
    return 0


def _combo(args) -> int:
    config = load_config(args.config)
    if not database_exists(config.download.db_path):
        raise ReleaseHarborError(
            "ledger database does not exist; run the demo or collect command first"
        )
    request = load_json_file(Path(args.request))
    connection = connect(config.download.db_path)
    try:
        clock = Clock.from_settings(config.clock_mode, config.clock_fixed)
        outcome = run_combo_query(config, connection, clock, request)
    finally:
        connection.close()
    decision = outcome.decision
    print(f"decision: {decision['decision_id']}")
    print(f"contract: {decision['contract_id']}")
    print(f"status: {decision['status']} (saved={outcome.saved})")
    print(f"rule_table: {decision['rule_table']['version']}")
    print(f"feasible_solutions: {len(decision['solutions'])}")
    for key, path in outcome.outputs.items():
        print(f"{key}: {path}")
    if decision["status"] != "deliverable":
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "demo":
            return _demo(args)
        if args.command == "collect":
            return _collect(args)
        if args.command == "serve":
            return _serve(args)
        if args.command == "combo":
            return _combo(args)
        raise ReleaseHarborError(f"unknown command: {args.command}")
    except (ReleaseHarborError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
