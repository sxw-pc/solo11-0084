"""Strict configuration loading and path-boundary validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .clock import format_utc
from .errors import ConfigError, SecurityBoundaryError
from .security import AddressContract, UrlPolicy, require_loopback_bind
from .util import canonical_json, ensure_directory, load_json_file, resolve_under, sha256_bytes

_CHANNEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}\Z")
_CONTRACTS = frozenset({AddressContract.LITERAL.value, AddressContract.PINNED_NAME.value})


@dataclass(frozen=True)
class PortalSettings:
    fixture: Path
    bind_host: str
    base_url: str
    address_contract: AddressContract = AddressContract.LITERAL


@dataclass(frozen=True)
class DiscoverySettings:
    max_datasets: int
    max_versions: int
    max_artifacts: int
    max_pages: int
    channels: tuple[str, ...]


@dataclass(frozen=True)
class DownloadSettings:
    work_dir: Path
    artifact_dir: Path
    quarantine_dir: Path
    db_path: Path
    report_dir: Path
    retries: int
    retry_backoff_ms: int
    chunk_size: int
    max_artifact_bytes: int


@dataclass(frozen=True)
class ConsoleSettings:
    port: int


@dataclass(frozen=True)
class AppConfig:
    source_path: Path
    root: Path
    raw: dict
    fingerprint: str
    portal: PortalSettings
    discovery: DiscoverySettings
    download: DownloadSettings
    console: ConsoleSettings
    clock_mode: str
    clock_fixed: str
    request_timeout: float
    reset_on_start: bool

    def ensure_output_dirs(self) -> None:
        for path in (
            self.download.work_dir,
            self.download.artifact_dir,
            self.download.quarantine_dir,
            self.download.db_path.parent,
            self.download.report_dir,
        ):
            ensure_directory(path)


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    return value


def _keys(value: dict, label: str, required: set[str], optional: set[str] = frozenset()) -> None:
    missing = required - set(value)
    unknown = set(value) - required - set(optional)
    if missing:
        raise ConfigError(f"{label} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise ConfigError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")


def _string(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _integer(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{label} must be an integer in [{low}, {high}]")
    return value


def _boolean(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean")
    return value


def _fixed_timestamp(value, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ConfigError(f"{label} must include a timezone")
    return format_utc(parsed.astimezone(timezone.utc))


def _safe_path(root: Path, raw_value: str, label: str) -> Path:
    try:
        return resolve_under(root, raw_value, label)
    except SecurityBoundaryError as exc:
        raise ConfigError(str(exc)) from exc


def _load_portal(raw: dict, root: Path) -> tuple[Path, str, str, AddressContract]:
    """Validate the portal origin, bind host, and address contract together."""
    portal_raw = _object(raw["portal"], "portal")
    _keys(portal_raw, "portal", {"fixture", "base_url"}, {"bind_host", "address_contract"})
    fixture = _safe_path(root, _string(portal_raw["fixture"], "portal.fixture"), "portal.fixture")
    if not fixture.is_file():
        raise ConfigError(f"portal fixture does not exist: {fixture}")
    base_url = _string(portal_raw["base_url"], "portal.base_url").rstrip("/")
    contract_raw = portal_raw.get("address_contract", AddressContract.LITERAL.value)
    if not isinstance(contract_raw, str) or contract_raw not in _CONTRACTS:
        raise ConfigError("portal.address_contract must be 'literal' or 'pinned_name'")
    contract = AddressContract(contract_raw)
    try:
        parsed_base = urlsplit(base_url)
        UrlPolicy(base_url, contract=contract)
        if parsed_base.path not in {"", "/"} or parsed_base.query or parsed_base.fragment:
            raise SecurityBoundaryError(
                "portal.base_url must be an origin without path, query, or fragment"
            )
    except (ValueError, SecurityBoundaryError) as exc:
        raise ConfigError(f"invalid portal.base_url: {exc}") from exc
    try:
        default_bind = parsed_base.hostname or ""
        bind_host = require_loopback_bind(
            _string(portal_raw.get("bind_host", default_bind), "portal.bind_host")
        )
        if contract is AddressContract.LITERAL and bind_host == "localhost":
            raise SecurityBoundaryError(
                "the literal address contract does not allow the 'localhost' bind host"
            )
    except SecurityBoundaryError as exc:
        raise ConfigError(f"invalid portal.bind_host: {exc}") from exc
    return fixture, bind_host, base_url, contract

def _validate_layout(source: Path, fixture: Path, download: DownloadSettings) -> None:
    work = download.work_dir.resolve()
    for path, label in (
        (download.artifact_dir, "artifact_dir"),
        (download.quarantine_dir, "quarantine_dir"),
        (download.db_path, "db_path"),
    ):
        resolved = path.resolve()
        if resolved == work or not resolved.is_relative_to(work):
            raise ConfigError(f"download.{label} must live below download.work_dir")
    for output in (
        download.work_dir,
        download.artifact_dir,
        download.quarantine_dir,
        download.report_dir,
        download.db_path,
    ):
        for protected in (source, fixture):
            resolved_output = output.resolve()
            resolved_input = protected.resolve()
            if resolved_output == resolved_input or resolved_input.is_relative_to(
                resolved_output
            ):
                raise ConfigError(f"generated output path would contain an input file: {output}")


def load_config(path_value: str | Path) -> AppConfig:
    path = Path(path_value).expanduser().resolve()
    try:
        raw = load_json_file(path)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    raw = _object(raw, "configuration")
    _keys(raw, "configuration",
          {"schema_version", "root", "portal", "discovery", "download", "security", "clock"},
          {"console", "demo"})
    if raw["schema_version"] != 1:
        raise ConfigError("schema_version must be 1")

    root_value = _string(raw["root"], "root")
    root = (path.parent / root_value).resolve()
    if not root.is_dir():
        raise ConfigError(f"project root does not exist: {root}")

    fixture, bind_host, base_url, contract = _load_portal(raw, root)

    discovery_raw = _object(raw["discovery"], "discovery")
    _keys(discovery_raw, "discovery",
          {"max_datasets", "max_versions", "max_artifacts", "max_pages", "channels"})
    channels_raw = discovery_raw["channels"]
    if not isinstance(channels_raw, list) or not 1 <= len(channels_raw) <= 10:
        raise ConfigError("discovery.channels must be a non-empty list")
    channels: list[str] = []
    for channel in channels_raw:
        if not isinstance(channel, str) or not _CHANNEL.fullmatch(channel):
            raise ConfigError(f"invalid discovery channel: {channel!r}")
        if channel not in channels:
            channels.append(channel)

    download_raw = _object(raw["download"], "download")
    _keys(download_raw, "download", {
        "work_dir", "artifact_dir", "quarantine_dir", "db_path", "report_dir",
        "retries", "retry_backoff_ms", "chunk_size", "max_artifact_bytes",
    })
    download = DownloadSettings(
        work_dir=_safe_path(root, _string(download_raw["work_dir"], "download.work_dir"), "work_dir"),
        artifact_dir=_safe_path(
            root, _string(download_raw["artifact_dir"], "download.artifact_dir"), "artifact_dir"
        ),
        quarantine_dir=_safe_path(
            root, _string(download_raw["quarantine_dir"], "download.quarantine_dir"), "quarantine_dir"
        ),
        db_path=_safe_path(root, _string(download_raw["db_path"], "download.db_path"), "db_path"),
        report_dir=_safe_path(
            root, _string(download_raw["report_dir"], "download.report_dir"), "report_dir"
        ),
        retries=_integer(download_raw["retries"], "download.retries", 0, 10),
        retry_backoff_ms=_integer(
            download_raw["retry_backoff_ms"], "download.retry_backoff_ms", 0, 10_000
        ),
        chunk_size=_integer(download_raw["chunk_size"], "download.chunk_size", 1024, 1_048_576),
        max_artifact_bytes=_integer(
            download_raw["max_artifact_bytes"], "download.max_artifact_bytes", 1, 52_428_800
        ),
    )

    security_raw = _object(raw["security"], "security")
    _keys(security_raw, "security", {"request_timeout_seconds"})
    timeout_raw = security_raw["request_timeout_seconds"]
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
        raise ConfigError("security.request_timeout_seconds must be numeric")
    if not 0.1 <= float(timeout_raw) <= 60.0:
        raise ConfigError("security.request_timeout_seconds must be in [0.1, 60.0]")

    clock_raw = _object(raw["clock"], "clock")
    _keys(clock_raw, "clock", {"mode"}, {"fixed"})
    mode = _string(clock_raw["mode"], "clock.mode")
    if mode not in {"fixed", "system"}:
        raise ConfigError("clock.mode must be 'fixed' or 'system'")
    fixed = "2026-01-15T12:00:00Z"
    if mode == "fixed":
        if "fixed" not in clock_raw:
            raise ConfigError("clock.fixed is required in fixed mode")
        fixed = _fixed_timestamp(clock_raw["fixed"], "clock.fixed")

    console_raw = _object(raw.get("console", {"port": 18766}), "console")
    _keys(console_raw, "console", {"port"})
    console = ConsoleSettings(
        port=_integer(console_raw["port"], "console.port", 1, 65535)
    )
    demo_raw = _object(raw.get("demo", {"reset_on_start": False}), "demo")
    _keys(demo_raw, "demo", {"reset_on_start"})
    reset = _boolean(demo_raw["reset_on_start"], "demo.reset_on_start")
    _validate_layout(path, fixture, download)

    return AppConfig(
        source_path=path,
        root=root,
        raw=raw,
        fingerprint=sha256_bytes(canonical_json(raw).encode("utf-8"))[:16],
        portal=PortalSettings(fixture, bind_host, base_url, contract),
        discovery=DiscoverySettings(
            _integer(discovery_raw["max_datasets"], "discovery.max_datasets", 1, 200),
            _integer(discovery_raw["max_versions"], "discovery.max_versions", 1, 100),
            _integer(discovery_raw["max_artifacts"], "discovery.max_artifacts", 1, 200),
            _integer(discovery_raw["max_pages"], "discovery.max_pages", 1, 2000),
            tuple(channels),
        ),
        download=download,
        console=console,
        clock_mode=mode,
        clock_fixed=fixed,
        request_timeout=float(timeout_raw),
        reset_on_start=reset,
    )
