"""Capacity-contract execution: ledger rebuild, admission, ordered updates.

The runner rebuilds the capacity ledger from actual file sizes/digests
plus the persisted event state, plans a complete update order, and then
guards every write, quarantine, and restore against the configured byte
ceiling.  Interruption leftovers are reconciled conservatively: residual
``.part`` bytes and quarantined files stay occupied, retired generations
are never reported available, and a verified row whose bytes are gone is
downgraded instead of being counted as free capacity or a new success.
"""

from __future__ import annotations

import json

from .capacity import UpdateTask, measure_workspace, plan_updates
from .downloader import artifact_destination
from .errors import CapacityAdmissionError, IntegrityError
from .util import relative_posix, sha256_file
from .verify import verify_file

_SOURCE_VERDICTS = {"checksum_mismatch", "size_mismatch"}


class CapacityRunner:
    def __init__(self, config, connection, ledger, processor, run_id, divergence) -> None:
        self.config = config
        self.connection = connection
        self.ledger = ledger
        self.processor = processor
        self.run_id = run_id
        self.divergence = divergence
        self.settings = config.capacity

    def _emit(self, code: str, subject: str, detail: dict, level: str = "info") -> None:
        self.ledger.record_event(self.run_id, code, subject, detail, level)

    def _occupancy(self) -> int:
        return measure_workspace(
            self.config.download.artifact_dir, self.config.download.quarantine_dir
        ).total

    def execute(self, refs) -> None:
        tasks, skips = self._build_update_tasks(refs)
        for ref in skips:
            self._process_safe(ref, None)
        if not tasks:
            return
        by_uid = {ref.artifact_uid: ref for ref in refs}
        plan = plan_updates(
            tasks, self._occupancy(), self.settings.workspace_bytes, self.settings.contract
        )
        self._emit_plan(plan)
        if not plan.feasible:
            self._block_all(list(tasks), plan)
            return
        self._execute_plan(list(plan.steps), by_uid)

    def _retirement_evidence(self, uid: str) -> dict | None:
        row = self.connection.execute(
            "SELECT code, detail_json FROM events WHERE subject = ? AND code IN "
            "('capacity.retired', 'capacity.restored', 'capacity.restore_failed') "
            "ORDER BY event_id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        if row is None or row["code"] == "capacity.restored":
            return None
        try:
            return json.loads(row["detail_json"])
        except json.JSONDecodeError:
            return {}

    def _build_update_tasks(self, refs) -> tuple[list[UpdateTask], list]:
        tasks: list[UpdateTask] = []
        skips: list = []
        for ref in refs:
            row = self.ledger.upsert_artifact(self.run_id, ref)
            uid = ref.artifact_uid
            target = artifact_destination(
                self.config.download.artifact_dir, ref.dataset_id, ref.version_id, ref.filename
            )
            part = target.with_name(target.name + ".part")
            part_bytes = part.stat().st_size if part.is_file() else 0
            exists = target.is_file()
            matches = False
            if exists:
                try:
                    matches = verify_file(target, ref.expected_size, ref.expected_sha256).accepted
                except IntegrityError:
                    matches = False
            status = row["status"]
            if matches:
                if status not in {"verified", "skipped"}:
                    self.ledger.update_artifact(
                        uid, self.run_id, status="verified",
                        local_path=relative_posix(self.config.root, target),
                        actual_size=ref.expected_size, actual_sha256=ref.expected_sha256.lower(),
                        error_code=None, error_detail=None, quarantine_path=None,
                    )
                    self._emit("capacity.reconciled", uid, {
                        "from": status, "to": "verified",
                        "reason": "target matches the expected digest",
                    })
                skips.append(ref)
                continue
            if not exists:
                evidence = self._retirement_evidence(uid)
                if status == "retired" or evidence is not None:
                    if status != "retired":
                        self.ledger.update_artifact(
                            uid, self.run_id, status="retired", local_path=None,
                            error_code="output_retired",
                            error_detail="verified output was retired before replacement",
                        )
                        self._emit("capacity.reconciled", uid, {
                            "from": status, "to": "retired",
                            "reason": "retirement evidence with missing target",
                        }, "warning")
                elif status in {"verified", "skipped"}:
                    self.ledger.update_artifact(
                        uid, self.run_id, status="failed", local_path=None,
                        error_code="output_missing",
                        error_detail="verified output is missing from the workspace",
                    )
                    self._emit("capacity.reconciled", uid, {
                        "from": status, "to": "failed",
                        "reason": "verified output missing without retirement evidence",
                    }, "warning")
            old_bytes = target.stat().st_size if exists else 0
            tasks.append(
                UpdateTask(
                    uid, ref.dataset_id, ref.version_id, ref.filename,
                    old_bytes, part_bytes, ref.expected_size, ref.url,
                )
            )
        return tasks, skips

    def _process_safe(self, ref, guard) -> str:
        try:
            return self.processor.process(ref, capacity=guard)
        except Exception as exc:  # keep one bad artifact from aborting the run
            detail = {
                "dataset": ref.dataset_id,
                "version": ref.version_id,
                "filename": ref.filename,
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.ledger.record_event(self.run_id, "artifact.failed", ref.artifact_uid, detail, "error")
            try:
                self.ledger.update_artifact(
                    ref.artifact_uid, self.run_id, status="failed", local_path=None,
                    error_code="internal_error", error_detail=str(exc),
                )
            except Exception:
                pass
            return "failed"

    def _emit_plan(self, plan) -> None:
        detail = {
            "contract": plan.contract,
            "capacity": plan.capacity,
            "occupancy": plan.base_occupancy,
            "feasible": plan.feasible,
            "exact": plan.exact,
            "steps": [
                {
                    "artifact": step.task.artifact_uid,
                    "retire_old": step.retire_old,
                    "old_bytes": step.task.old_bytes,
                    "part_bytes": step.task.part_bytes,
                    "new_bytes": step.task.new_bytes,
                    "peak": step.peak,
                    "occupancy_after": step.occupancy_after,
                }
                for step in plan.steps
            ],
        }
        if plan.feasible:
            detail["peak"] = plan.peak
            detail["final_occupancy"] = plan.final_occupancy
        self._emit("capacity.plan", "capacity", detail)

    def _block_all(self, tasks, plan) -> None:
        for task in tasks:
            self.ledger.update_artifact(
                task.artifact_uid, self.run_id, status="capacity_blocked",
                error_code="capacity_rejected",
                error_detail=f"workspace capacity {plan.capacity} admits no complete order",
            )
        self._emit("capacity.rejected", "capacity", {
            "reason": "no_feasible_order",
            "contract": plan.contract,
            "capacity": plan.capacity,
            "occupancy": plan.base_occupancy,
            "blocked": [task.artifact_uid for task in tasks],
            "lower_bounds": plan.lower_bounds,
        }, "warning")

    def _settle(self, step, guard, outcome: str) -> None:
        if not step.retire_old or not guard.retired:
            return
        uid = step.task.artifact_uid
        old_sha = guard.old_sha256
        if old_sha is None:
            evidence = self._retirement_evidence(uid)
            old_sha = evidence.get("old_sha256") if evidence else None
        if outcome == "verified":
            self._emit("capacity.restored", uid, {
                "artifact": uid, "old_sha256": old_sha, "available": True,
            })
            return
        self._emit("capacity.restore_failed", uid, {
            "artifact": uid, "old_sha256": old_sha,
            "outcome": outcome, "old_available": False,
        }, "warning")
        if outcome not in _SOURCE_VERDICTS:
            row = self.ledger.get_artifact(uid)
            self.ledger.update_artifact(
                uid, self.run_id, status="retired",
                error_code=row["error_code"], error_detail=row["error_detail"],
            )

    def _refresh_task(self, task: UpdateTask) -> UpdateTask:
        """Re-measure a task's old/part bytes from the actual workspace."""
        target = artifact_destination(
            self.config.download.artifact_dir, task.dataset_id, task.version_id, task.filename
        )
        part = target.with_name(target.name + ".part")
        return UpdateTask(
            task.artifact_uid, task.dataset_id, task.version_id, task.filename,
            target.stat().st_size if target.is_file() else 0,
            part.stat().st_size if part.is_file() else 0,
            task.new_bytes, task.source_url,
        )

    def _execute_plan(self, steps, by_uid) -> None:
        position = 0
        deferred: set[str] = set()
        while position < len(steps):
            step = steps[position]
            ref = by_uid[step.task.artifact_uid]
            self.divergence.clear()
            guard = _StepGuard(self, step)
            outcome = self._process_safe(ref, guard)
            measured = self._occupancy()
            if outcome == "deferred":
                reasons, remaining = ["admission drift before download"], steps[position:]
                if step.task.artifact_uid in deferred:
                    self._block_all([item.task for item in remaining], plan_updates(
                        [self._refresh_task(item.task) for item in remaining],
                        measured, self.settings.workspace_bytes, self.settings.contract,
                    ))
                    return
                deferred.add(step.task.artifact_uid)
            else:
                self._settle(step, guard, outcome)
                reasons = list(self.divergence)
                if measured != step.occupancy_after:
                    reasons.append(
                        f"occupancy {measured} differs from planned {step.occupancy_after}"
                    )
                if outcome not in {"verified", "skipped"}:
                    reasons.append(f"outcome {outcome}")
                remaining = steps[position + 1:]
            if not reasons:
                position += 1
                continue
            tasks = [self._refresh_task(item.task) for item in remaining]
            plan = plan_updates(
                tasks, measured, self.settings.workspace_bytes, self.settings.contract
            )
            self._emit("capacity.replan", "capacity", {
                "reasons": reasons,
                "occupancy": measured,
                "remaining": [task.artifact_uid for task in tasks],
                "feasible": plan.feasible,
            })
            if not plan.feasible:
                self._block_all(tasks, plan)
                return
            steps = list(plan.steps)
            position = 0


class _StepGuard:
    """Per-step admission: enforces the planned peak on every write."""

    def __init__(self, runner: CapacityRunner, step) -> None:
        self.runner = runner
        self.step = step
        self.retired = False
        self.old_sha256 = None

    def admit_download(self, ref) -> int:
        runner = self.runner
        task = self.step.task
        config = runner.config
        capacity = runner.settings.workspace_bytes
        occupancy = runner._occupancy()
        others = occupancy - task.part_bytes
        if self.step.retire_old:
            others -= task.old_bytes
        if capacity - others < task.new_bytes:
            raise CapacityAdmissionError(
                "capacity_drift",
                f"workspace drifted: budget {capacity - others} below declared {task.new_bytes}",
                replan=True,
            )
        if self.step.retire_old:
            self.retired = True
            target = artifact_destination(
                config.download.artifact_dir, ref.dataset_id, ref.version_id, ref.filename
            )
            if target.is_file():
                old_size = target.stat().st_size
                self.old_sha256 = sha256_file(target)
                runner.ledger.update_artifact(
                    task.artifact_uid, runner.run_id, status="retired", local_path=None,
                    actual_size=old_size, actual_sha256=self.old_sha256,
                    error_code=None, error_detail=None,
                )
                runner._emit("capacity.retired", task.artifact_uid, {
                    "artifact": task.artifact_uid,
                    "old_sha256": self.old_sha256,
                    "old_size": old_size,
                    "source_url": task.source_url,
                    "contract": runner.settings.contract,
                    "unavailable": True,
                }, "warning")
                target.unlink()
                others -= old_size - task.old_bytes
        limit = capacity - others
        runner._emit("capacity.step", task.artifact_uid, {
            "artifact": task.artifact_uid,
            "part_limit": limit,
            "occupancy": occupancy,
            "planned_peak": self.step.peak,
            "retire_old": self.step.retire_old,
        })
        return limit
