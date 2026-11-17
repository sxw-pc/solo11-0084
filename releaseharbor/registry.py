"""Versioned local registry of fictional publisher organizations.

A registry maps portal display names to candidate legal organization IDs by
effective date, declares mutual-exclusion literals over (display name,
organization) assignments, and records organization-to-group membership.
Registries are immutable inputs: a revision is a new file with a new
``registry_version``, so old analyses always replay against old rules.
All organizations, groups, and aliases are fictional.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .errors import AttributionError
from .util import canonical_json, load_json_file, sha256_bytes

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def is_day(value: str) -> bool:
    """Return True for real calendar dates in strict YYYY-MM-DD form."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Organization:
    org_id: str
    legal_name: str
    group_id: str | None


@dataclass(frozen=True)
class AliasEntry:
    display_name: str
    valid_from: str
    valid_to: str | None
    candidates: tuple[str, ...]

    def covers(self, day: str) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True)
class Exclusion:
    literals: tuple[tuple[str, str], ...]
    note: str


@dataclass(frozen=True)
class Registry:
    version: str
    fingerprint: str
    organizations: dict[str, Organization]
    groups: dict[str, str]
    aliases: tuple[AliasEntry, ...]
    exclusions: tuple[Exclusion, ...]

    def resolve(self, display_name: str, day: str) -> AliasEntry | None:
        for entry in self.aliases:
            if entry.display_name == display_name and entry.covers(day):
                return entry
        return None


def _day(value, label: str) -> str:
    if not is_day(value):
        raise AttributionError(f"{label} must be a real YYYY-MM-DD date")
    return value


def _text(entry, key: str) -> str:
    value = entry.get(key) if isinstance(entry, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise AttributionError(f"registry entry needs a non-empty string: {key}")
    return value.strip()


def _keys(entry, label, required, optional=frozenset()) -> None:
    if not isinstance(entry, dict):
        raise AttributionError(f"{label} must be an object")
    missing = required - set(entry)
    unknown = set(entry) - required - set(optional)
    if missing or unknown:
        problems = sorted(missing) + [f"unexpected:{key}" for key in sorted(unknown)]
        raise AttributionError(f"{label} keys invalid: {', '.join(problems)}")


def _entries(raw: dict, key: str, required: bool) -> list:
    value = raw.get(key, [] if not required else None)
    if not isinstance(value, list):
        raise AttributionError(f"registry.{key} must be a list")
    return value


def _organizations(raw: dict, groups: dict[str, str]) -> dict[str, Organization]:
    organizations: dict[str, Organization] = {}
    for entry in _entries(raw, "organizations", required=True):
        _keys(entry, "organization", {"id", "legal_name"}, {"group"})
        org_id = _text(entry, "id")
        group_id = entry.get("group")
        if group_id is not None and group_id not in groups:
            raise AttributionError(f"organization {org_id} uses unknown group {group_id!r}")
        if org_id in organizations:
            raise AttributionError(f"duplicate organization id: {org_id}")
        organizations[org_id] = Organization(org_id, _text(entry, "legal_name"), group_id)
    return organizations


def _aliases(raw: dict, organizations: dict[str, Organization]) -> tuple[AliasEntry, ...]:
    aliases: list[AliasEntry] = []
    for entry in _entries(raw, "aliases", required=True):
        _keys(entry, "alias", {"display_name", "valid_from", "candidates"}, {"valid_to"})
        name = _text(entry, "display_name")
        start = _day(entry.get("valid_from"), "alias.valid_from")
        end = None if entry.get("valid_to") is None else _day(entry["valid_to"], "alias.valid_to")
        if end is not None and end < start:
            raise AttributionError(f"alias {name!r} ends before it starts")
        candidates = entry.get("candidates")
        if (
            not isinstance(candidates, list)
            or not candidates
            or any(candidate not in organizations for candidate in candidates)
        ):
            raise AttributionError(f"alias {name!r} needs known candidate organizations")
        if len(set(candidates)) != len(candidates):
            raise AttributionError(f"alias {name!r} repeats a candidate")
        aliases.append(AliasEntry(name, start, end, tuple(sorted(candidates))))
    by_name: dict[str, list[AliasEntry]] = {}
    for entry in aliases:
        by_name.setdefault(entry.display_name, []).append(entry)
    for name, entries in by_name.items():
        entries.sort(key=lambda item: item.valid_from)
        for previous, current in zip(entries, entries[1:]):
            if previous.valid_to is None or current.valid_from <= previous.valid_to:
                raise AttributionError(f"alias ranges overlap for {name!r}")
    return tuple(aliases)


def _exclusions(raw: dict, organizations: dict[str, Organization]) -> tuple[Exclusion, ...]:
    exclusions: list[Exclusion] = []
    for entry in _entries(raw, "exclusions", required=False):
        _keys(entry, "exclusion", {"literals"}, {"note"})
        literals = entry.get("literals")
        if not isinstance(literals, list) or len(literals) < 2:
            raise AttributionError("an exclusion needs at least two literals")
        pairs = []
        for literal in literals:
            _keys(literal, "exclusion literal", {"display_name", "org_id"})
            org_id = _text(literal, "org_id")
            if org_id not in organizations:
                raise AttributionError(f"exclusion uses unknown organization {org_id!r}")
            pairs.append((_text(literal, "display_name"), org_id))
        exclusions.append(Exclusion(tuple(pairs), str(entry.get("note", ""))))
    return tuple(exclusions)


def load_registry(path: Path) -> Registry:
    """Load and validate one immutable, versioned publisher registry."""
    try:
        raw = load_json_file(path)
    except (OSError, ValueError) as exc:
        raise AttributionError(f"cannot read publisher registry {path}: {exc}") from exc
    _keys(
        raw,
        "registry",
        {"schema_version", "registry_version", "organizations", "aliases"},
        {"groups", "exclusions"},
    )
    if raw["schema_version"] != 1:
        raise AttributionError("registry schema_version must be 1")
    version = raw["registry_version"]
    if not isinstance(version, str) or not version.strip():
        raise AttributionError("registry_version must be a non-empty string")
    groups: dict[str, str] = {}
    for entry in _entries(raw, "groups", required=False):
        _keys(entry, "group", {"id", "name"})
        group_id = _text(entry, "id")
        if group_id in groups:
            raise AttributionError(f"duplicate group id: {group_id}")
        groups[group_id] = _text(entry, "name")
    organizations = _organizations(raw, groups)
    fingerprint = sha256_bytes(canonical_json(raw).encode("utf-8"))[:16]
    return Registry(
        version.strip(),
        fingerprint,
        organizations,
        groups,
        _aliases(raw, organizations),
        _exclusions(raw, organizations),
    )
