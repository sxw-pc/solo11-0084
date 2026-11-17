"""Independent audit output for fictional combination decisions.

These writers never touch the intake report files (``reports/report.*``); they
emit a separate, deterministic decision record plus a human-readable Markdown
audit and a JSONL event trail under the configured ``combo.decision_dir``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .util import atomic_write_text, ensure_directory, relative_posix

_STATUS_TITLE = {
    "deliverable": "DELIVERABLE (fictional rules only)",
    "infeasible": "INFEASIBLE - no branch combination satisfies the contract",
    "undecidable": "UNDECIDABLE - gates failed; no permission is granted by default",
}


def _decision_path(directory: Path, decision_id: str) -> Path:
    safe = "".join(char for char in decision_id if char.isalnum() or char in ".-_")
    if not safe or safe != decision_id:
        raise ValueError(f"unsafe decision id: {decision_id!r}")
    return directory / f"{safe}.json"


def write_decision_record(directory: Path, decision: dict) -> dict[str, str]:
    ensure_directory(directory)
    decision_id = decision["decision_id"]
    json_path = _decision_path(directory, decision_id)
    markdown_path = directory / f"{decision_id}.md"
    audit_path = directory / "combo-audit.jsonl"
    atomic_write_text(
        json_path,
        json.dumps(decision, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
    )
    atomic_write_text(markdown_path, _markdown(decision))
    event = {
        "decision_id": decision_id,
        "contract_id": decision["contract_id"],
        "status": decision["status"],
        "rule_table_version": decision["rule_table"]["version"],
        "input_digest": decision["input_digest"],
        "solutions": len(decision["solutions"]),
        "gate_failures": len(decision["gate_failures"]),
        "conflicts": len(decision["conflicts"]),
    }
    with audit_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
    return {
        "decision_json": json_path,
        "decision_markdown": markdown_path,
        "combo_audit_jsonl": audit_path,
    }


def _markdown(decision: dict) -> str:
    rule = decision["rule_table"]
    lines = [
        "# Fictional combination decision (offline, synthetic rules only)",
        "",
        f"- Decision: `{decision['decision_id']}`",
        f"- Contract: `{decision['contract_id']}`",
        f"- Status: **{_STATUS_TITLE.get(decision['status'], decision['status'])}**",
        f"- Rule table: `{rule['version']}` (fingerprint `{rule['fingerprint'][:12]}…`)",
        f"- Input digest: `{decision['input_digest']}`",
        "",
        f"> {decision['fictional_notice']}",
        "",
        "## Sources with provenance and rule version",
        "",
        "| # | Artifact UID | Dataset / version / file | Fictional label | Ledger | Disk recheck |",
        "|---:|---|---|---|---|---|",
    ]
    for source in sorted(decision["sources"], key=lambda item: item["position"]):
        recheck = source.get("disk_recheck")
        if isinstance(recheck, dict) and "error" in recheck:
            disk = f"error: {recheck['error']}"
        elif isinstance(recheck, dict):
            disk = (
                f"size_ok={recheck['size_ok']} digest_ok={recheck['digest_ok']} "
                f"sha256={recheck['actual_sha256'][:12]}…"
            )
        else:
            disk = "not re-checked"
        lines.append(
            f"| {source['position']} | `{source['artifact_uid']}` | "
            f"{source.get('dataset_id', '?')} / {source.get('version_id', '?')} / "
            f"{source.get('filename', '?')} | `{source['fictional_label']}` | "
            f"{source.get('ledger_status')} | {disk} |"
        )
    if decision["gate_failures"]:
        lines.extend(["", "## Gate failures (undecidable, never default-allow)", ""])
        for failure in decision["gate_failures"]:
            subject = failure["artifact_uid"] or "(request)"
            lines.append(f"- `{failure['code']}` — {subject}: {failure['detail']}")
    if decision["conflicts"]:
        lines.extend(["", "## Minimal conflict explanation", ""])
        for conflict in decision["conflicts"]:
            involved = ", ".join(f"`{uid}`" for uid in conflict["artifact_uids"])
            if conflict["kind"] == "mutually_exclusive_attribution_templates":
                lines.append(
                    f"- {conflict['kind']} in group `{conflict['exclusive_group']}`: "
                    f"templates {conflict['templates']} clash across sources {involved}"
                )
            else:
                lines.append(
                    f"- {conflict['kind']}: {conflict['obligation_types']} across {involved}"
                )
    if decision["solutions"]:
        lines.extend(["", "## Non-dominated feasible obligation sets", ""])
        for solution in decision["solutions"]:
            lines.append(f"### `{solution['solution_id']}`")
            lines.append("")
            for obligation in solution["obligations"]:
                template = obligation["template"] or "-"
                witnesses = ", ".join(f"`{uid}`" for uid in obligation["witness_uids"])
                lines.append(
                    f"- `{obligation['type']}` (template `{template}`) required by {witnesses}"
                )
            lines.append("")
            lines.append("Branch selection witnesses:")
            lines.append("")
            for witness in solution["selection"]:
                lines.append(
                    f"- `{witness['artifact_uid']}` label `{witness['fictional_label']}` "
                    f"-> branch `{witness['branch_id']}` (rule table "
                    f"`{witness['rule_table_version']}`)"
                )
            lines.append("")
    lines.append("End of fictional combination decision. No real-license legal conclusion.\n")
    return "\n".join(lines)


def relative_outputs(config, paths: dict[str, Path]) -> dict[str, str]:
    return {key: relative_posix(config.root, path) for key, path in paths.items()}
