"""Bounded branch-and-combination search for fictional use contracts.

The search enumerates every contract-eligible branch per source within an
explicit bound, merges each full selection into one unified obligation set,
rejects selections with conflicts instead of unioning them, and returns all
non-dominated feasible obligation sets with per-source witnesses.
"""

from __future__ import annotations

from itertools import product

from .combo_models import (
    ComboDecision,
    ComboSource,
    GateFailure,
    Witness,
    contract_blockers,
    decision_identity,
    input_digest,
    merge_choices,
    minimal_conflicts,
    pareto_solutions,
)
from .combo_request import load_combo_request
from .combo_rules import Branch, RuleTable
from .errors import RuleTableError
from .util import canonical_json, stable_id
from .combo_gate import gate_sources


def eligible_branches(
    source: ComboSource, rule_table: RuleTable, contract_id: str
) -> tuple[list[Branch], list[GateFailure]]:
    try:
        rule = rule_table.resolve_label(source.fictional_label)
    except RuleTableError as exc:
        return [], [GateFailure("unknown_fictional_label", source.artifact_uid, str(exc))]
    blockers = contract_blockers(contract_id)
    eligible = [branch for branch in rule.branches if not (branch.types() & blockers)]
    if not eligible:
        return [], [
            GateFailure(
                "no_contract_eligible_branch",
                source.artifact_uid,
                f"label {source.fictional_label!r} has no branch compatible with contract "
                f"{contract_id!r}; blocked obligation types: {sorted(blockers)}",
            )
        ]
    return eligible, []


def _witness(source: ComboSource, branch: Branch, evidence: list[dict], version: str) -> dict:
    record = next(item for item in evidence if item["artifact_uid"] == source.artifact_uid)
    return Witness(
        source.artifact_uid,
        record["dataset_id"],
        record["version_id"],
        record["filename"],
        source.fictional_label,
        branch.label,
        branch.branch_id,
        version,
    ).__dict__


def _feasible_solution(choices, evidence, rule_table, decision_id: str, index: int) -> dict:
    elements, _conflicts = merge_choices(choices, rule_table)
    ordered = [elements[key] for key in sorted(elements)]
    return {
        "obligation_keys": frozenset(element["obligation_key"] for element in ordered),
        "obligation_digest": stable_id(
            canonical_json(
                [
                    {"type": element["type"], "template": element["template"]}
                    for element in ordered
                ]
            )
        ),
        "obligations": [
            {
                "type": element["type"],
                "template": element["template"],
                "witness_uids": element["witness_uids"],
            }
            for element in ordered
        ],
        "selection": [
            _witness(source, branch, evidence, rule_table.version)
            for source, branch in choices
        ],
    }


def adjudicate(
    request: dict,
    rule_table: RuleTable,
    connection,
    root,
    *,
    max_sources: int,
    max_combinations: int,
) -> ComboDecision:
    contract_id, pinned_version, sources = load_combo_request(request)
    digest = input_digest(contract_id, rule_table.version, sources)
    decision_id = decision_identity(contract_id, rule_table.version, digest)
    gate: list[GateFailure] = []
    if pinned_version is not None and pinned_version != rule_table.version:
        gate.append(
            GateFailure(
                "stale_rule_table_version",
                None,
                f"request pins rule table version {pinned_version!r} but the controlled local "
                f"rule table is {rule_table.version!r}",
            )
        )
    if len(sources) > max_sources:
        gate.append(
            GateFailure(
                "too_many_sources",
                None,
                f"{len(sources)} sources exceed combo.max_sources={max_sources}",
            )
        )
    evidence, acceptance_failures = gate_sources(sources, connection, root)
    gate.extend(acceptance_failures)
    per_source_branches: list[list[Branch]] = []
    for source in sources:
        eligible, failures = eligible_branches(source, rule_table, contract_id)
        gate.extend(failures)
        per_source_branches.append(eligible)
    if gate:
        return ComboDecision(
            decision_id, contract_id, "undecidable", rule_table.version,
            rule_table.fingerprint, digest, evidence,
            [failure.__dict__ for failure in gate],
        )
    combination_space = 1
    for branches in per_source_branches:
        combination_space *= len(branches)
    if combination_space > max_combinations:
        return ComboDecision(
            decision_id, contract_id, "undecidable", rule_table.version,
            rule_table.fingerprint, digest, evidence,
            [
                GateFailure(
                    "unbounded_combination",
                    None,
                    f"branch combination space {combination_space} exceeds "
                    f"combo.max_combinations={max_combinations}",
                ).__dict__
            ],
        )
    raw_solutions: list[dict] = []
    all_conflicts: list[dict] = []
    for branches in product(*per_source_branches):
        choices = list(zip(sources, branches))
        _elements, conflicts = merge_choices(choices, rule_table)
        if conflicts:
            for conflict in conflicts:
                if conflict not in all_conflicts:
                    all_conflicts.append(conflict)
            continue
        raw_solutions.append(
            _feasible_solution(choices, evidence, rule_table, decision_id, len(raw_solutions))
        )
    if not raw_solutions:
        return ComboDecision(
            decision_id, contract_id, "infeasible", rule_table.version,
            rule_table.fingerprint, digest, evidence, [], [],
            minimal_conflicts(all_conflicts),
        )
    solutions = pareto_solutions(raw_solutions)
    for index, solution in enumerate(solutions):
        solution["solution_id"] = f"{decision_id}-s{index + 1:02d}"
        del solution["obligation_keys"]
        del solution["obligation_digest"]
    return ComboDecision(
        decision_id, contract_id, "deliverable", rule_table.version,
        rule_table.fingerprint, digest, evidence, [], solutions,
    )
