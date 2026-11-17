"""Dataclasses and pure set operations for fictional combination adjudication.

All semantics here are synthetic ``FICT-*`` test constructs. Nothing in this
module interprets a real license or produces a legal conclusion for real use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .combo_rules import Branch, RuleTable, _HARD_CONFLICTS
from .util import canonical_json, stable_id

CONTRACT_INTERNAL = "internal_aggregate_analysis"
CONTRACT_PUBLIC = "public_redistribution"
CONTRACTS = frozenset({CONTRACT_INTERNAL, CONTRACT_PUBLIC})
ACCEPTED_LEDGER_STATUSES = frozenset({"verified", "skipped"})
FICTIONAL_NOTICE = (
    "FICTIONAL RULES ONLY: synthetic FICT-* labels from a local controlled rule "
    "table; no real license interpretation and no legal conclusion for real use."
)
# Branch-level obligations that disqualify a branch for each fictional contract.
_CONTRACT_BLOCKERS = {
    CONTRACT_INTERNAL: frozenset(),  # no_public is permitted for local analysis
    CONTRACT_PUBLIC: frozenset({"no_public", "no_derivatives"}),
}


@dataclass(frozen=True)
class ComboSource:
    artifact_uid: str
    fictional_label: str
    expected_sha256: str


@dataclass(frozen=True)
class Witness:
    artifact_uid: str
    dataset_id: str
    version_id: str
    filename: str
    fictional_label: str
    label_resolved: str
    branch_id: str
    rule_table_version: str


@dataclass
class GateFailure:
    code: str
    artifact_uid: str | None
    detail: str


@dataclass
class ComboDecision:
    decision_id: str
    contract_id: str
    status: str  # deliverable | infeasible | undecidable
    rule_table_version: str
    rule_table_fingerprint: str
    input_digest: str
    sources: list[dict]
    gate_failures: list[dict] = field(default_factory=list)
    solutions: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    notice: str = FICTIONAL_NOTICE

    @property
    def deliverable(self) -> bool:
        return self.status == "deliverable"

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "decision_id": self.decision_id,
            "contract_id": self.contract_id,
            "status": self.status,
            "fictional_notice": self.notice,
            "rule_table": {
                "version": self.rule_table_version,
                "fingerprint": self.rule_table_fingerprint,
            },
            "input_digest": self.input_digest,
            "sources": self.sources,
            "gate_failures": self.gate_failures,
            "solutions": self.solutions,
            "conflicts": self.conflicts,
        }


def input_digest(contract_id: str, rule_version: str, sources: list[ComboSource]) -> str:
    payload = [
        contract_id,
        rule_version,
        [
            [item.artifact_uid, item.fictional_label, item.expected_sha256]
            for item in sources
        ],
    ]
    return stable_id(canonical_json(payload))


def decision_identity(contract_id: str, rule_version: str, digest: str) -> str:
    return "combo-" + stable_id(contract_id, rule_version, digest, length=20)


def contract_blockers(contract_id: str) -> frozenset[str]:
    return _CONTRACT_BLOCKERS[contract_id]


def merge_choices(
    choices: list[tuple[ComboSource, Branch]], rule_table: RuleTable
) -> tuple[dict[str, dict], list[dict]]:
    """Merge one full branch selection into a single unified obligation set.

    Returns the merged obligation elements and every conflict found. A simple
    union that silently dropped mutually exclusive obligations is never used:
    conflicts are reported so the caller can reject the whole selection.
    """
    elements: dict[str, dict] = {}
    conflicts: list[dict] = []
    for source, branch in choices:
        for obligation in branch.obligations:
            key = obligation.key()
            element = elements.setdefault(
                key,
                {
                    "type": obligation.type,
                    "template": obligation.template,
                    "obligation_key": key,
                    "witness_uids": [],
                },
            )
            if source.artifact_uid not in element["witness_uids"]:
                element["witness_uids"].append(source.artifact_uid)
    types = frozenset(element["type"] for element in elements.values())
    for conflict in _HARD_CONFLICTS:
        if conflict <= types:
            involved = sorted(
                source.artifact_uid
                for source, branch in choices
                if branch.types() & conflict
            )
            conflicts.append(
                {
                    "kind": "incompatible_obligations",
                    "obligation_types": sorted(conflict),
                    "artifact_uids": involved,
                }
            )
    groups: dict[str, list[str]] = {}
    for element in elements.values():
        if element["template"] is None:
            continue
        template = rule_table.templates[element["template"]]
        if template.exclusive_group is None:
            continue
        groups.setdefault(template.exclusive_group, []).append(element["template"])
    for group, template_ids in groups.items():
        distinct = sorted(set(template_ids))
        if len(distinct) > 1:
            involved = sorted(
                source.artifact_uid
                for source, branch in choices
                if any(
                    obligation.template in distinct
                    for obligation in branch.obligations
                )
            )
            conflicts.append(
                {
                    "kind": "mutually_exclusive_attribution_templates",
                    "exclusive_group": group,
                    "templates": distinct,
                    "artifact_uids": involved,
                }
            )
    return elements, conflicts


def pareto_solutions(solutions: list[dict]) -> list[dict]:
    """Keep every non-dominated feasible obligation set.

    Solution A dominates B only when A's obligation set is a strict subset of
    B's. Sets that differ in kind (neither contains the other) are both kept,
    so rule-table ordering can never silently pick one license as the only one.
    """
    kept: list[dict] = []
    for candidate in solutions:
        candidate_keys = candidate["obligation_keys"]
        if not any(
            other is not candidate and other["obligation_keys"] < candidate_keys
            for other in solutions
        ):
            kept.append(candidate)
    kept.sort(key=lambda item: (len(item["obligation_keys"]), item["obligation_digest"]))
    return kept


def minimal_conflicts(conflicts: list[dict]) -> list[dict]:
    """Smallest conflict explanations, measured by distinct sources involved."""
    if not conflicts:
        return []
    minimum = min(len(set(item["artifact_uids"])) for item in conflicts)
    chosen = [
        item for item in conflicts if len(set(item["artifact_uids"])) == minimum
    ]
    chosen.sort(key=lambda item: canonical_json(item))
    return chosen
