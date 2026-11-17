"""Standalone cross-version transit report rendering (JSON and Markdown)."""

from __future__ import annotations

from pathlib import Path

from .util import atomic_write_text, canonical_json, ensure_directory, relative_posix


def _contract_value(contract: dict) -> str:
    if contract.get("unknown"):
        return "unknown"
    return f"{contract['value']} ({contract['minimum']}..{contract['maximum']})"


def _witness_lines(witness: dict | None) -> list[str]:
    if not isinstance(witness, dict):
        return []
    lines = [
        f"  - {witness['label']} witness: cross-zone flow = {witness['cross_flow']}"
        f" ({witness['old_zone']} -> {witness['new_zone']}), checks ok = "
        f"{witness['checks']['ok']}",
        "",
        "    | Edge | Flow | Bounds |",
        "    |---|---:|---|",
    ]
    for edge in witness["edges"]:
        lines.append(
            f"    | {edge['key']} | {edge['flow']} | [{edge['min']}, {edge['max']}] |"
        )
    lines.append("")
    return lines


def transit_markdown(result: dict) -> str:
    lines = [
        "# Cross-version transit bounds",
        "",
        f"- Dataset: `{result['dataset']}`",
        f"- Direction: `{result['direction']}` "
        f"(base `{result['old_version']}` -> next `{result['new_version']}`)",
        f"- Input digest: `{result['input_digest']}`",
        f"- Note: {result['notes']}",
        "",
        "| Contract | Value | Minimum | Maximum |",
        "|---|---:|---:|---:|",
    ]
    for contract in result["contracts"]:
        lines.append(
            f"| {contract['contract']} | {_contract_value(contract)} | "
            f"{'' if contract['minimum'] is None else contract['minimum']} | "
            f"{'' if contract['maximum'] is None else contract['maximum']} |"
        )
    lines.extend(["", "## Evidence", ""])
    for item in result["evidence"]:
        lines.append(
            f"- {item['role']}: `{item['version']}/{item['filename']}` "
            f"size={item['expected_size']} sha256=`{item['expected_sha256']}` "
            f"path=`{item['local_path']}`"
        )
    for item in result["edge_evidence"]:
        lines.append(
            f"- edges: `{item['version']}/{item['filename']}` edges={item['edge_count']} "
            f"sha256=`{item['observed_sha256']}` version_date={item['version_date']}"
        )
    if result["diagnostics"]:
        lines.extend(["", "## Diagnostics", ""])
        for item in result["diagnostics"]:
            lines.append(
                f"- `{item['code']}` {item['role']}/{item['filename']} "
                f"line={item['line']}: {item['detail']}"
            )
    if result.get("conflict"):
        conflict = result["conflict"]
        lines.extend(["", "## Infeasibility certificate", ""])
        if conflict.get("new_station_shortfalls"):
            lines.append("| Station | Zone | Demand | Declared inflow cap | Shortfall |")
            lines.append("|---|---|---:|---:|---:|")
            for item in conflict["new_station_shortfalls"]:
                lines.append(
                    f"| {item['station']} | {item['zone']} | {item['demand']} | "
                    f"{item['declared_inflow_upper']} | {item['shortfall']} |"
                )
        if conflict.get("old_station_shortfalls"):
            lines.append("")
            lines.append("| Station | Zone | Supply | Declared outflow cap | Shortfall |")
            lines.append("|---|---|---:|---:|---:|")
            for item in conflict["old_station_shortfalls"]:
                lines.append(
                    f"| {item['station']} | {item['zone']} | {item['supply']} | "
                    f"{item['declared_outflow_upper']} | {item['shortfall']} |"
                )
        if conflict.get("unmet_demand"):
            lines.append("")
            for item in conflict["unmet_demand"]:
                lines.append(f"- residual unmet demand at `{item['node']}`: {item['unmet']}")
        if conflict.get("cut_edges"):
            lines.append("")
            for item in conflict["cut_edges"]:
                lines.append(
                    f"- cut edge `{item['key']}` [{item['lower']}, {item['upper']}] "
                    f"residual={item['residual']}"
                )
    for contract in result["contracts"]:
        if contract.get("witnesses"):
            lines.extend(["", f"## {contract['contract']} witnesses", ""])
            for name in ("minimum", "maximum"):
                witness = contract["witnesses"].get(name)
                if not witness:
                    continue
                lines.append(
                    f"### {name} ({witness['cross_flow']} "
                    f"{witness['old_zone']}->{witness['new_zone']})"
                )
                lines.extend(_witness_lines(witness))
    lines.append("")
    return "\n".join(lines)


def write_transit_result(config, result: dict, kind: str = "transit") -> dict[str, str]:
    """Write a standalone transit report without touching intake artifacts.

    The latest result is written under the report directory; every result is
    also archived under its input digest in a durable area outside the
    regenerated report tree, so a prior edge-table version can be re-read after
    a newer query (or a fresh demo run) has replaced the latest report.
    """
    report_directory = Path(config.download.report_dir) / "transit"
    ensure_directory(report_directory)
    payload = canonical_json(result) + "\n"
    stem = kind
    json_path = report_directory / f"{stem}.json"
    markdown_path = report_directory / f"{stem}.md"
    atomic_write_text(json_path, payload)
    atomic_write_text(markdown_path, transit_markdown(result))
    digest = result.get("input_digest") or "unknown"
    archive_directory = Path(config.download.work_dir) / "transit-results"
    archived_json = archive_directory / f"{stem}-{digest}.json"
    atomic_write_text(archived_json, payload)
    return {
        "transit_json": relative_posix(config.root, json_path),
        "transit_markdown": relative_posix(config.root, markdown_path),
        "transit_archive_json": relative_posix(config.root, archived_json),
    }
