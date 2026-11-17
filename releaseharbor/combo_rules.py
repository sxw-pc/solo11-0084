"""Controlled, versioned fictional rule tables for offline combination adjudication.

The rule table is loaded only from a local, configuration-controlled file. Portal
license text never reaches this module and can never become an executable rule.
All labels, branches, templates, and obligations are synthetic ``FICT-*`` test
constructs; nothing here interprets a real license (CC or otherwise) or produces a
legal conclusion for any real use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import RuleTableError
from .util import canonical_json, load_json_file, sha256_bytes

_OBLIGATION_TYPES = frozenset(
    {"attribution", "share_alike", "no_public", "no_derivatives"}
)
_LABEL = re.compile(r"FICT-[A-Z0-9][A-Z0-9._-]{0,59}\Z")
_VERSION = re.compile(r"[a-z0-9][a-z0-9._-]{1,59}\Z")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,59}\Z")

# Fictional machine semantics: these obligation types cannot coexist, both inside
# one branch and across the unified combination obligation set.
_HARD_CONFLICTS = (frozenset({"no_derivatives", "share_alike"}),)


@dataclass(frozen=True)
class Obligation:
    type: str
    template: str | None = None

    def key(self) -> str:
        return self.type if self.template is None else f"{self.type}:{self.template}"


@dataclass(frozen=True)
class Branch:
    branch_id: str
    label: str
    obligations: tuple[Obligation, ...]
    rule_table_version: str

    def types(self) -> frozenset[str]:
        return frozenset(item.type for item in self.obligations)


@dataclass(frozen=True)
class LabelRule:
    label: str
    branches: tuple[Branch, ...]


@dataclass(frozen=True)
class AttributionTemplate:
    id: str
    slot: str
    exclusive_group: str | None
    text: str


@dataclass(frozen=True)
class RuleTable:
    version: str
    fingerprint: str
    max_branches_per_label: int
    templates: dict[str, AttributionTemplate] = field(default_factory=dict)
    labels: dict[str, LabelRule] = field(default_factory=dict)
    fictional_notice: str = ""

    def resolve_label(self, label: str) -> LabelRule:
        visited: list[str] = []
        current = label
        while current not in self.labels:
            if current in visited:  # defensive: cycles are rejected at load time
                raise RuleTableError(f"alias cycle involving {label!r}")
            visited.append(current)
            current = self._aliases.get(current)
            if current is None:
                raise RuleTableError(f"unknown fictional label: {label!r}")
        return self.labels[current]

    _aliases: dict[str, str] = field(default_factory=dict)


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise RuleTableError(f"{label} must be an object")
    return value


def _text(value, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleTableError(f"{label} must be a non-empty string")
    text = value.strip()
    if pattern is not None and not pattern.fullmatch(text):
        raise RuleTableError(f"{label} has an invalid value: {value!r}")
    return text


def _check_branch_obligations(raw_obligations, branch_label: str, templates: dict) -> tuple:
    if not isinstance(raw_obligations, list) or not raw_obligations:
        raise RuleTableError(f"{branch_label}.obligations must be a non-empty list")
    obligations: list[Obligation] = []
    seen_types: set[str] = set()
    for index, raw in enumerate(raw_obligations):
        item = _object(raw, f"{branch_label}.obligations[{index}]")
        if set(item) != {"type"} and set(item) != {"type", "template"}:
            raise RuleTableError(f"{branch_label}.obligations[{index}] has unknown keys")
        otype = _text(item.get("type"), f"{branch_label}.obligations[{index}].type", _ID)
        if otype not in _OBLIGATION_TYPES:
            raise RuleTableError(
                f"{branch_label}.obligations[{index}] uses unknown obligation type {otype!r}"
            )
        if otype in seen_types:
            raise RuleTableError(
                f"{branch_label} repeats obligation type {otype!r}; branches must not branch on "
                "duplicate obligations"
            )
        seen_types.add(otype)
        template_id = item.get("template")
        if otype == "attribution":
            template_id = _text(
                template_id, f"{branch_label}.obligations[{index}].template", _ID
            )
            if template_id not in templates:
                raise RuleTableError(
                    f"{branch_label} references unknown attribution template {template_id!r}"
                )
        elif template_id is not None:
            raise RuleTableError(
                f"{branch_label}.obligations[{index}] only attribution may name a template"
            )
        obligations.append(Obligation(otype, template_id))
    types = frozenset(seen_types)
    for conflict in _HARD_CONFLICTS:
        if conflict <= types:
            raise RuleTableError(
                f"{branch_label} is self-contradictory: {sorted(conflict)} cannot coexist"
            )
    return tuple(obligations), types


def _load_templates(raw: list, version: str) -> dict[str, AttributionTemplate]:
    if not isinstance(raw, list) or not raw:
        raise RuleTableError("rule table templates must be a non-empty list")
    templates: dict[str, AttributionTemplate] = {}
    for index, entry in enumerate(raw):
        item = _object(entry, f"templates[{index}]")
        if set(item) != {"id", "slot", "text"} and set(item) != {
            "id",
            "slot",
            "exclusive_group",
            "text",
        }:
            raise RuleTableError(f"templates[{index}] has unknown keys")
        template_id = _text(item["id"], f"templates[{index}].id", _ID)
        if template_id in templates:
            raise RuleTableError(f"duplicate template id: {template_id!r}")
        slot = _text(item["slot"], f"templates[{index}].slot", _ID)
        text = _text(item["text"], f"templates[{index}].text")
        if len(text) > 500:
            raise RuleTableError(f"templates[{index}].text is too long")
        group = item.get("exclusive_group")
        if group is not None:
            group = _text(group, f"templates[{index}].exclusive_group", _ID)
        templates[template_id] = AttributionTemplate(template_id, slot, group, text)
    return templates


def _load_aliases(raw, labels: set[str], seen_labels: set[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    if not isinstance(raw, list):
        raise RuleTableError("rule table aliases must be a list")
    for index, entry in enumerate(raw):
        item = _object(entry, f"aliases[{index}]")
        if set(item) != {"alias", "target"}:
            raise RuleTableError(f"aliases[{index}] has unknown keys")
        alias = _text(item["alias"], f"aliases[{index}].alias", _LABEL)
        target = _text(item["target"], f"aliases[{index}].target", _LABEL)
        if alias in labels or alias in seen_labels:
            raise RuleTableError(f"alias {alias!r} collides with a label or another alias")
        if target == alias:
            raise RuleTableError(f"alias {alias!r} points at itself")
        aliases[alias] = target
        seen_labels.add(alias)
    # Reject alias cycles and aliases that do not eventually reach a real label.
    for alias in aliases:
        chain = [alias]
        current = aliases[alias]
        while current in aliases:
            if current in chain:
                raise RuleTableError(
                    f"alias cycle: {' -> '.join(chain + [current])}"
                )
            chain.append(current)
            current = aliases[current]
        if current not in labels:
            raise RuleTableError(f"alias {alias!r} resolves to unknown label {current!r}")
    return aliases


def _load_labels(raw, version: str, cap: int, templates: dict) -> dict[str, LabelRule]:
    if not isinstance(raw, list) or not raw:
        raise RuleTableError("rule table labels must be a non-empty list")
    labels: dict[str, LabelRule] = {}
    for l_index, entry in enumerate(raw):
        item = _object(entry, f"labels[{l_index}]")
        if set(item) != {"label", "branches"}:
            raise RuleTableError(f"labels[{l_index}] has unknown keys")
        label = _text(item["label"], f"labels[{l_index}].label", _LABEL)
        if label in labels:
            raise RuleTableError(f"duplicate fictional label: {label!r}")
        raw_branches = item["branches"]
        if not isinstance(raw_branches, list) or not raw_branches:
            raise RuleTableError(f"{label}.branches must be a non-empty list")
        if len(raw_branches) > cap:
            raise RuleTableError(
                f"{label} declares {len(raw_branches)} branches, exceeding max_branches_per_label"
            )
        branches: list[Branch] = []
        seen_branch_ids: set[str] = set()
        for b_index, raw_branch in enumerate(raw_branches):
            branch_item = _object(raw_branch, f"{label}.branches[{b_index}]")
            if set(branch_item) != {"branch_id", "obligations"}:
                raise RuleTableError(f"{label}.branches[{b_index}] has unknown keys")
            branch_id = _text(
                branch_item["branch_id"], f"{label}.branches[{b_index}].branch_id", _ID
            )
            if branch_id in seen_branch_ids:
                raise RuleTableError(f"{label} has duplicate branch id {branch_id!r}")
            seen_branch_ids.add(branch_id)
            obligations, types = _check_branch_obligations(
                branch_item["obligations"], f"{label}.{branch_id}", templates
            )
            # Attribution templates in one exclusive group are mutually exclusive
            # even when they originate from the same branch.
            groups = [
                templates[obligation.template].exclusive_group
                for obligation in obligations
                if obligation.template is not None
                and templates[obligation.template].exclusive_group is not None
            ]
            if len(groups) != len(set(groups)):
                raise RuleTableError(
                    f"{label}.{branch_id} combines mutually exclusive attribution templates"
                )
            branches.append(Branch(branch_id, label, obligations, version))
        labels[label] = LabelRule(label, tuple(branches))
    return labels


def load_rule_table(path_value: str | Path) -> RuleTable:
    """Load and fully validate the controlled fictional rule table."""
    path = Path(path_value)
    try:
        raw = load_json_file(path, limit=1_000_000)
    except (OSError, ValueError) as exc:
        raise RuleTableError(f"cannot read rule table {path}: {exc}") from exc
    raw = _object(raw, "rule table")
    if set(raw) != {
        "schema_version",
        "rule_table_version",
        "description",
        "fictional_notice",
        "max_branches_per_label",
        "templates",
        "aliases",
        "labels",
    }:
        raise RuleTableError("rule table has unknown or missing top-level keys")
    if raw["schema_version"] != 1:
        raise RuleTableError("rule table schema_version must be 1")
    version = _text(raw["rule_table_version"], "rule_table_version", _VERSION)
    notice = _text(raw["fictional_notice"], "fictional_notice")
    cap = raw["max_branches_per_label"]
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 8:
        raise RuleTableError("max_branches_per_label must be an integer in [1, 8]")
    templates = _load_templates(raw["templates"], version)
    label_names = {
        _text(entry["label"], "label", _LABEL) for entry in raw["labels"]
    }
    aliases = _load_aliases(raw["aliases"], label_names, set(label_names))
    labels = _load_labels(raw["labels"], version, cap, templates)
    fingerprint = sha256_bytes(canonical_json(raw).encode("utf-8"))
    return RuleTable(
        version=version,
        fingerprint=fingerprint,
        max_branches_per_label=cap,
        templates=templates,
        labels=labels,
        fictional_notice=notice,
        _aliases=aliases,
    )
