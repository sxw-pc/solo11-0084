"""Business contracts over a cross-version integer flow bound result.

Two contracts consume the same feasible interval:

* ``guaranteed_relocation`` reports the smallest old-zone-to-new-zone flow over
  every feasible integer assignment, i.e. the volume that provably moved.
* ``maximum_exposure`` reports the largest such flow, i.e. the service capacity
  a planner must reserve.

Both always show the full feasible interval and both witnesses; when any input
is unusable both return ``unknown`` with a located reason instead of a number.
"""

from __future__ import annotations

from .transit_flow import Bounds
from .transit_model import TransitInputs
from .util import canonical_json, sha256_bytes

CONTRACTS = ("guaranteed", "maximum_exposure")
_LABELS = {
    "guaranteed": "guaranteed_relocation",
    "maximum_exposure": "maximum_exposure",
}


def evidence_digest(spec, inputs: TransitInputs) -> str:
    payload = {
        "dataset": spec.dataset_id,
        "old_version": spec.old_version,
        "new_version": spec.new_version,
        "old_zone": spec.old_zone,
        "new_zone": spec.new_zone,
        "evidence": inputs.evidence(),
        "edge_evidence": inputs.edge_evidence,
        "diagnostics": [item.as_dict() for item in inputs.diagnostics],
    }
    return sha256_bytes(canonical_json(payload).encode("utf-8"))[:24]


def _contract(name: str, direction: str, minimum, maximum, value, reasons, witnesses) -> dict:
    return {
        "contract": _LABELS[name],
        "direction": direction,
        "minimum": minimum,
        "maximum": maximum,
        "value": value,
        "unknown": value is None,
        "unknown_reason": reasons,
        "witnesses": witnesses,
    }


def envelope(spec, inputs, results, conflict=None, note="") -> dict:
    return {
        "schema_version": 1,
        "dataset": spec.dataset_id,
        "old_version": spec.old_version,
        "new_version": spec.new_version,
        "old_zone": spec.old_zone,
        "new_zone": spec.new_zone,
        "direction": f"{spec.old_zone}->{spec.new_zone}",
        "input_digest": evidence_digest(spec, inputs),
        "evidence": inputs.evidence(),
        "edge_evidence": inputs.edge_evidence,
        "notes": note,
        "contracts": results,
        "diagnostics": [item.as_dict() for item in inputs.diagnostics],
        "conflict": conflict,
    }


def infeasible_contracts(spec, inputs, conflict, note="") -> dict:
    """Both contracts go unknown, carrying the checkable cut/supply conflict."""
    direction = f"{spec.old_zone}->{spec.new_zone}"
    reason = {
        "code": "infeasible",
        "role": "flow",
        "filename": "",
        "line": None,
        "detail": conflict["detail"],
    }
    results = [
        _contract(name, direction, None, None, None, [reason], None) for name in CONTRACTS
    ]
    return envelope(spec, inputs, results, conflict=conflict, note=note)


def unknown_contracts(spec, inputs, reasons, note="") -> dict:
    direction = f"{spec.old_zone}->{spec.new_zone}"
    results = [
        _contract(name, direction, None, None, None, reasons, None) for name in CONTRACTS
    ]
    return envelope(spec, inputs, results, note=note)


def numeric_contracts(spec, inputs, bounds: Bounds, note="") -> dict:
    direction = f"{spec.old_zone}->{spec.new_zone}"
    witnesses = {"minimum": bounds.min_witness, "maximum": bounds.max_witness}
    results = [
        _contract(
            name,
            direction,
            bounds.minimum,
            bounds.maximum,
            bounds.minimum if name == "guaranteed" else bounds.maximum,
            [],
            witnesses,
        )
        for name in CONTRACTS
    ]
    return envelope(spec, inputs, results, note=note)
