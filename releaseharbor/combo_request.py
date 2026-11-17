"""Strict parsing of offline combination requests."""

from __future__ import annotations

from .combo_models import CONTRACTS, ComboSource
from .errors import ComboDecisionError
from .util import is_sha256


def load_combo_request(raw: dict) -> tuple[str, str | None, list[ComboSource]]:
    if not isinstance(raw, dict):
        raise ComboDecisionError("combination request must be an object")
    if set(raw) != {"contract_id", "rule_table_version", "sources"}:
        raise ComboDecisionError("combination request has unknown or missing keys")
    contract_id = raw["contract_id"]
    if not isinstance(contract_id, str) or contract_id not in CONTRACTS:
        raise ComboDecisionError(f"unknown contract_id: {contract_id!r}")
    pinned = raw.get("rule_table_version")
    if pinned is not None and (not isinstance(pinned, str) or not pinned.strip()):
        raise ComboDecisionError("rule_table_version must be a non-empty string")
    raw_sources = raw["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ComboDecisionError("combination request needs at least one source")
    sources: list[ComboSource] = []
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict) or set(item) != {
            "artifact_uid",
            "fictional_label",
            "expected_sha256",
        }:
            raise ComboDecisionError(f"sources[{index}] has unknown or missing keys")
        uid = item["artifact_uid"]
        label = item["fictional_label"]
        digest = item["expected_sha256"]
        if not isinstance(uid, str) or not uid.strip():
            raise ComboDecisionError(f"sources[{index}].artifact_uid must be a string")
        if not isinstance(label, str) or not label.strip():
            raise ComboDecisionError(f"sources[{index}].fictional_label must be a string")
        if not is_sha256(digest):
            raise ComboDecisionError(f"sources[{index}].expected_sha256 is invalid")
        sources.append(ComboSource(uid, label.strip(), digest.lower()))
    return contract_id, (pinned.strip() if pinned else None), sources
