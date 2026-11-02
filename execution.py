"""Checkpointed DC/AC execution, bounded recovery, and cooperative stopping."""

from __future__ import annotations

from dataclasses import asdict
from collections import Counter
from pathlib import Path
import time

from .distributed import DistributedConfig, DistributedComputer
from .powerworld import PowerWorldError
from .recovery import RecoveryStore, StopRequested, contingency_table

# One StudyExecution owns one contingency group, not the whole case queue.
# Saved phases describe the NEXT work: dc -> ac -> outputs -> complete, or
# full_ac (with next_batch) -> outputs -> complete. Recovery follows those markers.


def is_worker_failure(error: Exception) -> bool:
    """Retry transport/pool failures, never every solver or input error."""
    text = str(error).casefold()
    # Explicit configuration/permission failures win over generic "unavailable"
    # text; repeatedly reconnecting will not repair those problems.
    if any(token in text for token in ("access is denied", "access denied", "authentication",
                                      "master password", "license", "not registered", "unknown field")):
        return False
    return any(token in text for token in (
        "no computers available", "no computers are available", "no distributed computers", "rpc server is unavailable",
        "rpc server unavailable", "remote procedure call failed", "object invoked has disconnected",
        "0x800706ba", "0x800706be", "0x80010108", "-2147023174", "-2147023170", "-2147417848",
    ))


def pool_for_stage(config: DistributedConfig, active: int, *, resize: bool, reduced: bool) -> DistributedConfig:
    """Build a stage pool without silently dropping configured computers.

    A small stage still gets spread across every configured computer when it
    has enough packets to do so.  The old first-fit allocation gave all
    packets to the first host(s), which made a 2,243-contingency AC stage use
    only two of ten selected computers.  A computer cannot receive a worker
    when there are fewer packets than computers, but otherwise each selected
    host receives at least one worker before spare workers are assigned.
    """
    packets = max(1, (active + config.contingencies_per_process - 1) // config.contingencies_per_process)
    # This is a static cap based on available work, not adaptive CPU monitoring.
    # Work is allocated in saved computer order, within each saved maximum.
    remaining = packets if resize else sum(c.connections for c in config.computers)
    candidates = config.computers
    if reduced and len(candidates) > 1:
        # If a small stage selected an unavailable first host, the recovery attempt
        # must also be able to try another configured machine.
        candidates = candidates[1:] + candidates[:1]
    capacities = [max(1, computer.connections // 2) if reduced else computer.connections
                  for computer in candidates]
    allocations = [0] * len(candidates)

    # Reserve one worker per host first.  This is the important difference
    # from first-fit allocation: all selected hosts participate in a stage
    # whenever the packet count makes that possible.
    if resize:
        for index, capacity in enumerate(capacities):
            if remaining <= 0:
                break
            allocations[index] = 1
            remaining -= 1

    # Fill in round-robin order so one host does not receive almost every
    # packet while the other selected hosts sit idle.
    while remaining > 0:
        progress = False
        for index, capacity in enumerate(capacities):
            if remaining <= 0:
                break
            if allocations[index] < capacity:
                allocations[index] += 1
                remaining -= 1
                progress = True
        if not progress:
            break

    computers = [
        DistributedComputer(computer.name, allocations[index])
        for index, computer in enumerate(candidates)
        if allocations[index] > 0
    ]
    return DistributedConfig(config.contingencies_per_process, tuple(computers))


class StudyExecution:
    def __init__(self, workflow, options, manifest: Path) -> None:
        self.host = workflow
        self.options = options
        self.store = RecoveryStore(manifest)
        self.progress = workflow.progress
        self.log = workflow.log
        self.warnings: list[str] = []

    def report(self, stage: str, **details) -> None:
        self.progress({"stage": stage, "checkpoint": str(self.store.path), **details})

    def stop_if_requested(self) -> None:
        if self.host.stop_requested():
            raise StopRequested(f"Stopped safely. Resume this group using {self.store.path}")

    def commit(self, **changes) -> None:
        # Advance the saved phase only with its matching case snapshot. A log
        # saying a solve returned is not itself a durable recovery checkpoint.
        state = {**self.store.data, **changes, "warnings": self.warnings}
        self.report("Saving recovery checkpoint")
        started = time.monotonic()
        self.store.commit(self.host.client, state)
        self.log(f"Recovery saved: {self.store.path} (next: {state['phase']}; "
                 f"checkpoint time={time.monotonic() - started:.1f}s).")

    def start(self, *, force_ac: bool, output_base: Path, delete_skipped: bool) -> None:
        phase = "full_ac" if self.options.full_ac else "ac" if force_ac else "dc"
        rows = contingency_table(self.host.client)
        if not rows:
            raise PowerWorldError("No contingencies were created for this group.")
        self.store.data = {
            # Keep original labels/skips so Full AC can mask each batch and then
            # restore the user's prepared study rather than leaving the last mask.
            "phase": phase, "options": asdict(self.options),
            "output_base": output_base.name, "delete_skipped": delete_skipped,
            "original_skips": [[row[0], row[1]] for row in rows], "next_batch": 0,
        }
        self.commit()
        self.run()

    def reopen(self, stage: str) -> None:
        self.report(f"Restarting PowerWorld for {stage}")
        self.host._restart_from_checkpoint(self.store.checkpoint, stage)
        self.store.validate_open_case(self.host.client)

    def set_skips(self, rows) -> None:
        # Bounded COM payloads, thousands of times fewer round trips for large lists.
        for start in range(0, len(rows), 2000):
            self.stop_if_requested()
            self.host.client.change_parameters_multiple(
                "Contingency", ("CTGLabel", "CTGSkip"), rows[start:start + 2000]
            )

    def solve(self, stage: str, *, ac: bool, batch_labels=None, preserve=False) -> None:
        original = self.host.settings.distributed_config() if self.options.use_distributed else None
        # Retry the full allocation in a fresh session before reducing capacity.
        attempts = ["distributed", "distributed", "reduced"] if original else ["local"]
        if original and self.host.settings.allow_local_fallback:
            attempts.append("local")
        for index, mode in enumerate(attempts):
            self.stop_if_requested()
            try:
                if index:
                    # Discard any partial failed attempt before trying again;
                    # do not trust results left in the failing COM session.
                    self.reopen(stage)
                self.host._configure_analysis(self.options, force_ac=ac)
                if batch_labels is not None:
                    active_set = set(batch_labels)
                    rows = [[label, "NO" if label in active_set else "YES"]
                            for label, _skip in self.store.data["original_skips"]]
                    self.set_skips(rows)
                active = sum(row[1].upper() == "NO" for row in contingency_table(self.host.client))
                if not active:
                    self.log(f"{stage}: no active contingencies; no worker pool is needed.")
                    return
                if mode != "local":
                    config = pool_for_stage(original, active, resize=self.host.settings.size_worker_pool,
                                            reduced=mode == "reduced")
                    self.host._active_distributed_config = config
                    self.host._apply_distributed_config(config)
                    self.report(f"Verifying workers for {stage}", active=active)
                else:
                    self.host._active_distributed_config = None
                if index:
                    self.log(f"Retrying {stage} using {mode} execution from the clean checkpoint.")
                if mode == "local" and original:
                    warning = f"{stage} used local fallback after distributed failures."
                    self.warnings.append(warning)
                    self.log(f"WARNING: {warning}")
                self.report(stage, active=active, mode=mode)
                stage_started = time.monotonic()
                self.host._run_solve_all(distributed=mode != "local", stage=stage, preserve_results=preserve)
                elapsed = time.monotonic() - stage_started
                results = contingency_table(self.host.client)
                # A completed command can include legitimately unsolved cases.
                # Report them as results warnings, not transport failures to retry.
                unresolved = [row for row in results if row[1].upper() == "NO" and row[3].upper() not in {"YES", "TRUE", "1"}]
                if unresolved:
                    warning = (f"{stage}: {len(unresolved):,} of {active:,} active contingencies "
                               f"did not solve ({len(unresolved) / active:.1%}).")
                    self.warnings.append(warning)
                    self.log(f"WARNING: {warning}")
                    # CTGSolved can describe an abort or other outcome, not just
                    # Newton nonconvergence. Preserve PowerWorld's actual text.
                    counts = Counter(row[3] or "(blank)" for row in unresolved)
                    self.log("PowerWorld unsolved statuses: " + "; ".join(
                        f"{status!r}: {count:,}" for status, count in counts.items()))
                    self.log("Unsolved examples: " + "; ".join(
                        f"{row[0]} [{row[3] or '(blank)'}]" for row in unresolved[:10]))
                self.log(
                    f"Completed {stage}: {active:,} active contingencies in "
                    f"{elapsed:.1f}s total stage time ({mode})."
                )
                return
            except StopRequested:
                self.stop_if_requested()
                raise
            except PowerWorldError as exc:
                self.log(f"{stage} attempt {index + 1}/{len(attempts)} failed: {exc}")
                if mode == "local" or not is_worker_failure(exc) or index == len(attempts) - 1:
                    raise PowerWorldError(
                        f"{stage} stopped: {exc}\nResume from {self.store.path}. "
                        "The last completed checkpoint is unchanged."
                    ) from exc
            finally:
                # Stage sizing/reduction must not leak into the next stage's
                # baseline. Saved user allocations remain the source of truth.
                self.host._active_distributed_config = None

    def run(self) -> None:
        self.warnings = list(self.store.data.get("warnings", []))
        self.stop_if_requested()
        phase = self.store.data["phase"]
        if phase == "dc":
            self.solve("DC screening", ac=False)
            unresolved = [[row[0], "NO"] for row in contingency_table(self.host.client)
                          if row[1].upper() == "NO" and row[3].upper() not in {"YES", "TRUE", "1"}]
            self.log("Skipping contingencies with no DC violations.")
            self.host.client.run_script_command('SetData(Contingency, [CTGSkip], ["YES"], "No_Violations");')
            # A failed DC solution is not proof that there are no AC violations.
            # Complete this bookkeeping even if Stop safely was requested during DC.
            for start in range(0, len(unresolved), 2000):
                self.host.client.change_parameters_multiple(
                    "Contingency", ("CTGLabel", "CTGSkip"), unresolved[start:start + 2000])
            rows = contingency_table(self.host.client)
            remaining = sum(row[1].upper() == "NO" for row in rows)
            self.log(f"DC screening complete: {remaining:,} of {len(rows):,} contingencies remain for AC.")
            self.commit(phase="ac")
            self.stop_if_requested()
            if remaining and self.options.use_distributed:
                self.log("Keeping the coordinator session for AC after DC screening; "
                         "worker availability will be verified again.")
            phase = "ac"
        if phase == "ac":
            self.solve("AC contingency analysis", ac=True)
            self.commit(phase="outputs")
        elif phase == "full_ac":
            # Full AC never screens out zero-DC-violation cases. Batch only the
            # originally active set, preserving previous batches' solve results.
            active = [label for label, skip in self.store.data["original_skips"] if skip.upper() == "NO"]
            size = self.options.contingencies_per_file
            count = (len(active) + size - 1) // size
            for batch in range(self.store.data["next_batch"], count):
                self.stop_if_requested()
                if batch and self.options.use_distributed:
                    self.log(f"Keeping the coordinator session for Full AC batch {batch + 1}/{count}.")
                self.report(f"Full AC batch {batch + 1}/{count}", completed=batch, total=count)
                self.solve(f"Full AC batch {batch + 1}/{count}", ac=True,
                           batch_labels=active[batch * size:(batch + 1) * size], preserve=batch > 0)
                self.commit(next_batch=batch + 1)
            # Batch skip flags were temporary; restore them before final output.
            self.set_skips(self.store.data["original_skips"])
            self.commit(phase="outputs")
        self.stop_if_requested()

    def finish(self):
        # Calculation completion and output publication are separate phases, so
        # an output-write failure can be resumed without recalculating the study.
        if self.store.data["phase"] != "outputs":
            raise PowerWorldError("Study has not completed its calculation stages.")
        if self.store.data["delete_skipped"]:
            self.host.client.run_script_command('Delete(Contingency, "SKIPPED");')
        self.report("Writing final outputs")
        base_name = self.store.data["output_base"]
        if Path(base_name).name != base_name:
            raise PowerWorldError("Invalid recovery output filename.")
        outputs = self.host._write_outputs(self.store.path.parent / base_name, self.options)
        self.store.data.update(phase="complete", outputs=[path.name for path in outputs], warnings=self.warnings)
        self.store.write()
        self.report("Completed with warnings" if self.warnings else "Complete")
        return outputs
