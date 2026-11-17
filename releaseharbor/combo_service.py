"""Application service for offline fictional combination queries."""

from __future__ import annotations

from dataclasses import dataclass

from .combo_reports import relative_outputs, write_decision_record
from .combo_rules import load_rule_table
from .combo_search import adjudicate
from .errors import ComboDecisionError
from .ledger import Ledger


@dataclass
class ComboOutcome:
    decision: dict
    saved: bool
    outputs: dict[str, str]


def run_combo_query(config, connection, clock, request: dict) -> ComboOutcome:
    if config.combo is None:
        raise ComboDecisionError(
            "combination queries are disabled; add a controlled 'combo' section to config"
        )
    rule_table = load_rule_table(config.combo.rule_table)
    decision = adjudicate(
        request,
        rule_table,
        connection,
        config.root,
        max_sources=config.combo.max_sources,
        max_combinations=config.combo.max_combinations,
    )
    payload = decision.to_dict()
    ledger = Ledger(connection, clock)
    saved = ledger.save_combo_decision(payload)
    paths = write_decision_record(config.combo.decision_dir, payload)
    return ComboOutcome(payload, saved, relative_outputs(config, paths))
