"""Schema-level acceptance for CSV artifacts after byte-level acceptance.

The service turns accepted CSV bytes plus the portal's declarative schemas into
immutable ledger records that separate "transfer complete" from "readable under
a named consumer contract". It never inspects sample values to infer types.
"""

from __future__ import annotations

from pathlib import Path

from .csvschema import (
    NAME_BOUND_CONTRACT,
    POSITIONAL_CONTRACT,
    SchemaDeclaration,
    adjudicate_compatibility,
    schema_lineage_key,
    validate_csv,
)
from .discovery import ArtifactRef
from .ledger import Ledger


class CsvSchemaService:
    def __init__(self, ledger: Ledger, run_id: str) -> None:
        self.ledger = ledger
        self.run_id = run_id

    def _lineage_for(self, refs: list[ArtifactRef]) -> dict[str, list[SchemaDeclaration]]:
        """Pre-register every declaration discovered in this run.

        All declarations land before any evaluation, so a predecessor published
        by a different version in the same run resolves deterministically.
        """
        lineages: dict[str, list[SchemaDeclaration]] = {}
        for ref in refs:
            if ref.media_type != "text/csv" or ref.schema is None:
                continue
            self.ledger.record_schema_declaration(self.run_id, ref)
            key = schema_lineage_key(ref.dataset_id, ref.filename)
            lineages.setdefault(key, [])
            if not any(
                item.fingerprint() == ref.schema.fingerprint()
                and item.schema_version == ref.schema.schema_version
                for item in lineages[key]
            ):
                lineages[key].append(ref.schema)
        return lineages

    def prime(self, refs: list[ArtifactRef]) -> None:
        self._lineage_for(refs)

    def _resolve_predecessor(
        self, lineage: str, declaration: SchemaDeclaration
    ) -> tuple[SchemaDeclaration | None, str | None]:
        if declaration.predecessor is None:
            return None, None
        previous = self.ledger.find_predecessor_declaration(
            lineage, declaration.predecessor
        )
        if previous is None:
            return None, (
                f"predecessor schema version {declaration.predecessor!r} is not "
                "published anywhere in this lineage"
            )
        if previous.fingerprint() == declaration.fingerprint():
            return previous, (
                f"predecessor reference {declaration.predecessor!r} resolves to the "
                "same declaration fingerprint as the current schema"
            )
        return previous, None

    def evaluate_accepted_artifact(
        self,
        ref: ArtifactRef,
        local_path: Path,
        *,
        accepted_sha256: str,
        accepted_size: int,
    ) -> None:
        """Validate one byte-accepted, schema-bearing text/csv artifact."""
        declaration = ref.schema
        assert declaration is not None
        lineage = schema_lineage_key(ref.dataset_id, ref.filename)
        self.ledger.record_schema_declaration(self.run_id, ref)
        previous, reference_error = self._resolve_predecessor(lineage, declaration)

        validation = validate_csv(local_path, declaration)
        verdicts = adjudicate_compatibility(previous, declaration)
        by_contract = {verdict.contract: verdict for verdict in verdicts}
        positional = by_contract[POSITIONAL_CONTRACT]
        name_bound = by_contract[NAME_BOUND_CONTRACT]

        # A declaration-level ruling must never be presented as proof that the
        # bytes are readable: failed validation or an unresolvable predecessor
        # nulls every compatibility flag rather than recording a false verdict.
        if not validation.ok or reference_error is not None:
            positional_compatible: bool | None = None
            name_bound_compatible: bool | None = None
        else:
            positional_compatible = positional.compatible
            name_bound_compatible = name_bound.compatible

        evaluation = {
            "lineage_key": lineage,
            "content_sha256": accepted_sha256,
            "content_size": accepted_size,
            "schema_version": declaration.schema_version,
            "declaration_fingerprint": declaration.fingerprint(),
            "declaration_summary": declaration.summary(),
            "predecessor_version": previous.schema_version if previous is not None else None,
            "predecessor_fingerprint": previous.fingerprint() if previous is not None else None,
            "predecessor_summary": previous.summary() if previous is not None else None,
            "change_reason": declaration.change_reason,
            "validation_ok": validation.ok,
            "validation": validation.to_dict(),
            "reference_error": reference_error,
            "positional_compatible": positional_compatible,
            "name_bound_compatible": name_bound_compatible,
            "contracts": [verdict.to_dict() for verdict in verdicts],
        }
        self.ledger.save_schema_evaluation(self.run_id, ref, evaluation)
