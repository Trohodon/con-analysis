"""Contingency construction and first-pass PowerWorld analysis workflow."""

from __future__ import annotations

import csv
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
import time
from typing import Callable
from uuid import uuid4

from .execution import StudyExecution, is_worker_failure
from .recovery import RecoveryStore, StopRequested

from .aux_files import (
    CATEGORY_LABELS,
    COMMON_STRUCTURE,
    GENERIC_CONTINGENCY,
    GENERIC_CONTINGENCY_POST,
    JOIN_ACTIVE_CONTINGENCY,
    MANUAL,
    OPERATING_GUIDE,
    paths_for_case,
)
from .powerworld import PowerWorldClient, PowerWorldError
from .preparation import apply_input_file
from .settings import AppSettings
from .distributed import (
    PowerWorldComputerStatus,
    parse_powerworld_computer_statuses,
    write_distributed_aux,
)

# This module prepares the electrical study and applies legacy group rules.
# execution.py handles stage progression/retries; powerworld.py handles COM.
# Keep that separation when adding a group so recovery need not know its AUX recipe.

LogCallback = Callable[[str], None]
BaseCaseDecision = Callable[[Path, int], bool]
BRANCH_RESULT_FIELDS = (
    "BusNum",
    "BusName",
    "BusNum:1",
    "BusName:1",
    "LineCircuit",
    "LineXfmr",
    "CTGViol",
    "LineMaxPercentContingency",
)
CSV_RESULT_FIELDS = (
    "CTGLabel",
    "LimViolID",
    "LimViolCat",
    "LimViolValue",
    "LimViolLimit",
    "LimViolPct",
    "LimViolCTGSpecifiedLimit",
    "CTGSolved",
)


@dataclass(slots=True)
class ContingencyOptions:
    # Describes what to build. Normally run_analysis splits these flags into
    # separate outputs; the preparation buttons can build a combined set instead.
    n1: bool = True
    n11: bool = True
    long_term_p2: bool = True
    extreme_double_bus: bool = False
    extreme_double_branch: bool = False
    extreme_bus_branch: bool = False
    extreme_p1_p7: bool = False
    use_model_build: bool = False
    write_n1_models: bool = False
    evaluate_nonexistent: bool = True

    def validate(self) -> None:
        if not any(
            (
                self.n1,
                self.n11,
                self.long_term_p2,
                self.extreme_double_bus,
                self.extreme_double_branch,
                self.extreme_bus_branch,
                self.extreme_p1_p7,
            )
        ):
            raise ValueError("Select at least one contingency group.")
        if self.write_n1_models and not self.use_model_build:
            raise ValueError("N-1 model creation requires the Model Build method.")
        if self.use_model_build:
            # Preserve the option's shape for settings/UI compatibility, but reject
            # execution until the legacy per-N-1 model workflow is fully ported.
            raise ValueError(
                "The Model Build method and N-1 model writer are not available in this test "
                "release. Use the validated Join Active method."
            )


@dataclass(slots=True)
class AnalysisOptions:
    # Describes how to solve/export, independent of which groups were selected.
    # This dataclass is serialized into recovery records; existing records must
    # be considered before renaming fields or changing their interpretation.
    mode: str = "dc_ac"
    dc_monitor_percent: int = 80
    ac_monitor_percent: int = 90
    screen_tdf_cutoff: float = 2.0
    screen_volt_cutoff: float = 2.5
    do_not_rereport: bool = True
    rereport_tdf_cutoff: float = 0.5
    rereport_volt_cutoff: float = 0.5
    check_basecase: bool = True
    full_ac: bool = False
    contingencies_per_file: int = 80_000
    use_distributed: bool = True
    delete_skipped: bool = True
    create_pwb: bool = True
    create_aux: bool = False
    create_csv: bool = False

    def validate(self) -> None:
        if self.mode not in {"dc_ac", "ac_screen"}:
            raise ValueError("Unknown analysis mode.")
        if self.mode == "ac_screen":
            raise ValueError(
                "AC Screening is not available in this test release. The legacy implementation "
                "requires balancing-area screening AUX files and contains unfinished processing. "
                "Use DC with AC verification."
            )
        if not 0 <= self.dc_monitor_percent <= 32767:
            raise ValueError("DC CA Lim Mon % must be between 0 and 32767.")
        if not 0 <= self.ac_monitor_percent <= 32767:
            raise ValueError("AC CA Lim Mon % must be between 0 and 32767.")
        if self.mode == "ac_screen" and not 0 < self.screen_tdf_cutoff <= 100:
            raise ValueError("Screen TDF cutoff must be greater than 0 and no more than 100.")
        if self.mode == "ac_screen" and not 0 < self.screen_volt_cutoff <= 20:
            raise ValueError("Screen Volt cutoff must be greater than 0 and no more than 20.")
        if self.do_not_rereport and self.rereport_tdf_cutoff < 0:
            raise ValueError("TDF % cutoff cannot be negative.")
        if self.do_not_rereport and self.rereport_volt_cutoff < 0:
            raise ValueError("Volt % cutoff cannot be negative.")
        if self.full_ac and self.contingencies_per_file < 1:
            raise ValueError("Contingencies/file must be a positive integer.")
        if not any((self.create_pwb, self.create_aux, self.create_csv)):
            raise ValueError("Select at least one output type.")


@dataclass(slots=True)
class AnalysisResult:
    case_path: Path
    outputs: tuple[Path, ...]
    warnings: tuple[str, ...] = ()


class BaseCaseViolationError(PowerWorldError):
    """Raised when the user declines to continue after the base-case check."""


class ContingencyWorkflow:
    def __init__(
        self,
        client: PowerWorldClient,
        settings: AppSettings,
        *,
        resources_dir: Path | None = None,
        log: LogCallback | None = None,
        confirm_basecase_violations: BaseCaseDecision | None = None,
        distributed_retry_delays: tuple[float, ...] = (5.0, 15.0, 30.0),
        distributed_pool_release_delay: float = 30.0,
        wait: Callable[[float], None] = time.sleep,
        client_factory: Callable[[], PowerWorldClient] = PowerWorldClient,
        progress: Callable[[dict], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.client = client
        # Freeze preferences for this workflow. GUI edits must not change worker
        # allocations or per-case AUX mappings halfway through a study.
        self.settings = deepcopy(settings)
        self.resources_dir = resources_dir or Path(__file__).resolve().parent.parent / "Resources"
        self.log = log or (lambda _message: None)
        self.confirm_basecase_violations = confirm_basecase_violations
        self.distributed_retry_delays = distributed_retry_delays
        self.distributed_pool_release_delay = distributed_pool_release_delay
        self.wait = wait
        # Injectable waits/clients let tests simulate outages without sleeping or
        # requiring a licensed installation. GUI communication uses callbacks only.
        self.client_factory = client_factory
        self.progress = progress or (lambda _event: None)
        self.stop_requested = stop_requested or (lambda: False)
        self._active_distributed_config = None
        self._runtime_aux: Path | None = None

    def prepare_combined_case(
        self,
        case_path: str | Path,
        output_path: str | Path,
        contingencies: ContingencyOptions,
    ) -> Path:
        contingencies.validate()
        case = Path(case_path)
        destination = Path(output_path)
        self.validate_inputs(case, contingencies)
        self._open_and_condition(case)
        try:
            self.build_contingencies(case, contingencies)
            self._log_contingency_count()
            self.log(f"Saving prepared contingency case: {destination}")
            self.client.save_case(destination, "PWB", True)
            return destination
        finally:
            self._close_safely()

    def write_pti_contingencies(
        self,
        case_path: str | Path,
        output_path: str | Path,
        contingencies: ContingencyOptions,
        *,
        truncate_labels: bool = False,
    ) -> Path:
        contingencies.validate()
        case = Path(case_path)
        destination = Path(output_path)
        self.validate_inputs(case, contingencies)
        self._open_and_condition(case)
        try:
            self.build_contingencies(case, contingencies)
            self._log_contingency_count()
            escaped = str(destination).replace('"', '""')
            truncate = "YES" if truncate_labels else "NO"
            self.client.run_script_command(f'CTGWriteFilePTI("{escaped}","Name12","{truncate}");')
            self.log(f"PTI contingency file written: {destination}")
            return destination
        finally:
            self._close_safely()

    def run_analysis(
        self,
        case_path: str | Path,
        output_dir: str | Path,
        contingencies: ContingencyOptions,
        analysis: AnalysisOptions,
    ) -> AnalysisResult:
        # One input case per call: validate -> optional base-case check -> groups.
        # Each group reloads the original case, not the previous group's output.
        contingencies.validate()
        analysis.validate()
        self.settings.use_distributed = analysis.use_distributed
        case = Path(case_path)
        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)
        self.validate_inputs(case, contingencies, include_basecase=analysis.check_basecase)
        self._log_aux_plan(case)
        self.log(f"Coordinator session {self.client.session_id}; successful groups/stages reuse this session.")
        self.log(f"Full AC batch size={analysis.contingencies_per_file:,}; "
                 f"distributed packet size={self.settings.contingencies_per_process:,}.")
        if analysis.check_basecase and analysis.mode == "dc_ac":
            try:
                self._check_basecase(case, analysis)
            except Exception:
                self.client.disconnect()
                raise
        outputs: list[Path] = []
        warnings: list[str] = []
        groups = self._selected_groups(contingencies)
        for index, (label, group) in enumerate(groups, start=1):
            if self.stop_requested():
                self.client.disconnect()
                raise StopRequested("Stopped before starting the next contingency group.")
            self.progress({"stage": f"Preparing group {index}/{len(groups)}: {label}"})
            if index > 1:
                self.log(f"Reusing coordinator session {self.client.session_id}; "
                         "reloading the original input case for the next group.")
            self.log(f"Starting contingency group {index} of {len(groups)}: {label}")
            execution = None
            try:
                # Establish a checkpoint before configuring/connecting the pool.
                self._open_and_condition(case, configure_workers=False)
                self.build_contingencies(case, group)
                if analysis.delete_skipped and not label.startswith("EE_"):
                    # The legacy workflow removes contingencies already marked SKIP
                    # before normal analysis. Its Extreme Events path leaves them in
                    # place through DC screening and removes them after AC instead.
                    self.client.run_script_command('Delete(Contingency, "SKIPPED");')
                self._log_contingency_count()
                force_ac = label in {"P1,2,4,7", "LongTerm"}
                # These two legacy groups go straight to AC even when the overall
                # selection is "DC with AC verification" for N-1-1.
                if label == "P1,2,4,7":
                    self._configure_n1_controls()
                self._configure_analysis(analysis, force_ac=force_ac)
                self.log(f"Running contingency analysis for {label}.")
                if analysis.full_ac or (analysis.mode == "dc_ac" and force_ac):
                    run_type = "ACCA"
                elif analysis.mode == "dc_ac":
                    run_type = "DCwACver"
                else:
                    run_type = "scrCA"
                base = output_root / f"{case.stem}_{run_type}_{label}"
                manifest = output_root / f"{base.name}_{uuid4().hex[:8]}.resume.json"
                # Final names stay legacy-compatible; recovery names are unique
                # so an interrupted earlier run is not overwritten by a new one.
                self._runtime_aux = manifest.with_suffix(".workers.aux")
                execution = StudyExecution(self, analysis, manifest)
                execution.start(force_ac=force_ac, output_base=base,
                                delete_skipped=analysis.delete_skipped and label != "P1,2,4,7")
                outputs.extend(execution.finish())
                warnings.extend(execution.warnings)
            finally:
                self._close_safely()
                complete = execution is not None and execution.store.data.get("phase") == "complete"
                # Close the case between groups, but retain a healthy coordinator.
                # Only completed groups may discard their temporary recovery files.
                if not complete or index == len(groups):
                    self.client.disconnect()
                if complete:
                    removed = execution.store.cleanup_completed()
                    if removed:
                        self.log(f"Removed {len(removed)} temporary recovery file(s).")
                self._runtime_aux = None
        return AnalysisResult(case, tuple(outputs), tuple(warnings))

    def resume_analysis(self, manifest: str | Path) -> AnalysisResult:
        # Resume one prepared group with its saved analysis choices. Current
        # settings supply the pool, allowing an unavailable host to be removed.
        # This does not reconstruct unstarted cases/groups from the original queue.
        store = RecoveryStore(Path(manifest))
        data = store.load()
        if data["phase"] == "complete":
            raise PowerWorldError("This recovery record is already complete. Start a new study to rerun it.")
        options = AnalysisOptions(**data["options"])
        options.validate()
        self.settings.use_distributed = options.use_distributed
        self._runtime_aux = store.path.with_suffix(".workers.aux")
        execution = StudyExecution(self, options, store.path)
        execution.store = store
        self.log(f"Resuming {store.path}; saved stage: {data['phase']}. Current computer allocations will be used.")
        try:
            self.client.open_case(store.checkpoint)
            store.validate_open_case(self.client)
            execution.run()
            outputs = execution.finish()
            return AnalysisResult(store.checkpoint, tuple(outputs), tuple(execution.warnings))
        finally:
            self._close_safely()
            self.client.disconnect()
            if execution.store.data.get("phase") == "complete":
                removed = execution.store.cleanup_completed()
                if removed:
                    self.log(f"Removed {len(removed)} temporary recovery file(s).")

    def _log_aux_plan(self, case: Path) -> None:
        self.log(f"AUX/CON selections for {case.name}:")
        for category, label in CATEGORY_LABELS.items():
            selected = paths_for_case(self.settings.aux_files, category, str(case))
            if selected:
                self.log(f"  {label}: " + " | ".join(selected))
            else:
                self.log(f"  {label}: none")

    def _check_basecase(self, case: Path, options: AnalysisOptions) -> int:
        """Run the legacy BASECASE contingency once before any selected groups."""
        self.log(f"Checking {case.name} for base-case violations before analysis.")
        self._open_and_condition(case, configure_workers=False)
        # A single BASECASE solve does not need the distributed pool. The check
        # uses the conditioned input, then closes it before group construction.
        try:
            self._apply_file(self.resources_dir / "Basecase.aux")
            line_percent = options.ac_monitor_percent
            if not options.full_ac:
                line_percent = min(options.dc_monitor_percent, options.ac_monitor_percent)
            self.client.change_parameters_single(
                "LimitSet",
                ("LSNum", "LSName", "LSLinePercent"),
                ("1", "Default", str(line_percent)),
            )
            self.client.run_script_command('CTGSolve("BASECASE");')
            result = self.client.get_parameters_multiple(
                "Contingency", ("CTGLabel", "CTGSkip", "CTGViol"), ""
            )
            try:
                labels, _skipped, violations = result[1]
            except (IndexError, TypeError, ValueError) as exc:
                raise PowerWorldError(
                    "PowerWorld returned an unexpected BASECASE result table."
                ) from exc

            violation_count: int | None = None
            for label, value in zip(labels, violations):
                if str(label).strip().casefold() == "basecase":
                    if value is None or str(value).strip() == "":
                        violation_count = 0
                    else:
                        violation_count = int(float(str(value)))
                    break
            if violation_count is None:
                raise PowerWorldError(
                    "PowerWorld did not return the BASECASE contingency after loading Basecase.aux."
                )

            self.log(f"Base case violations found: {violation_count}")
            if violation_count > 0:
                # Without an explicit decision callback, stop rather than silently
                # continuing a study the user expected to screen for base violations.
                should_continue = (
                    self.confirm_basecase_violations(case, violation_count)
                    if self.confirm_basecase_violations is not None
                    else False
                )
                if not should_continue:
                    raise BaseCaseViolationError(
                        f"Analysis stopped because {case.name} has {violation_count} base-case "
                        "violation(s). Correct them, or turn off the base-case check to continue."
                    )
                self.log("Continuing after the base-case violation warning.")
            return violation_count
        finally:
            self._close_safely()

    def validate_inputs(
        self,
        case: Path,
        options: ContingencyOptions,
        *,
        include_basecase: bool = False,
    ) -> None:
        # Check only inputs needed by selected groups and this case's AUX mapping.
        # Keep these requirements aligned with build_contingencies below.
        required: list[Path] = [case, self.resources_dir / "Conts_Filters.aux"]
        if include_basecase:
            required.append(self.resources_dir / "Basecase.aux")
        if options.n1 or options.n11 or options.extreme_p1_p7:
            required.append(self.resources_dir / "P1_All_Except_CB-CB.aux")
            required.extend(
                Path(path)
                for path in paths_for_case(self.settings.aux_files, GENERIC_CONTINGENCY, str(case))
            )
        if options.n1 or options.n11:
            required.append(self.resources_dir / "P2_All_Except_BF.aux")
            for category in (
                JOIN_ACTIVE_CONTINGENCY,
                GENERIC_CONTINGENCY_POST,
                COMMON_STRUCTURE,
            ):
                required.extend(
                    Path(path)
                    for path in paths_for_case(self.settings.aux_files, category, str(case))
                )
        if options.n11:
            if options.use_model_build:
                required.append(self.resources_dir / "N-2_Branches&TFMRs.aux")
            required.extend(
                Path(path)
                for path in paths_for_case(self.settings.aux_files, OPERATING_GUIDE, str(case))
            )
        if options.long_term_p2:
            required.append(self.resources_dir / "UG_Lines.aux")
            required.append(self.resources_dir / "P2_All_Except_BF.aux")
            required.extend(
                Path(path)
                for path in paths_for_case(
                    self.settings.aux_files,
                    JOIN_ACTIVE_CONTINGENCY,
                    str(case),
                )
            )
        if options.extreme_bus_branch or options.extreme_double_branch:
            required.append(self.resources_dir / "N-1_All.aux")
            required.extend(
                Path(path)
                for path in paths_for_case(self.settings.aux_files, GENERIC_CONTINGENCY, str(case))
            )
        if options.extreme_double_branch:
            required.append(self.resources_dir / "N-2_Branches&TFMRs.aux")
        if options.extreme_double_bus:
            required.append(self.resources_dir / "EE_DBL_Bus.aux")
        if options.extreme_bus_branch:
            required.append(self.resources_dir / "P2_Buses.aux")
        required.extend(
            Path(path)
            for path in paths_for_case(self.settings.aux_files, MANUAL, str(case))
        )
        if options.extreme_p1_p7:
            required.extend(
                Path(path)
                for path in paths_for_case(self.settings.aux_files, COMMON_STRUCTURE, str(case))
            )

        missing = list(dict.fromkeys(path for path in required if not path.is_file()))
        if missing:
            listing = "\n".join(f"  {path}" for path in missing)
            raise PowerWorldError(f"Required input files are missing or unavailable:\n{listing}")

    @staticmethod
    def _selected_groups(options: ContingencyOptions) -> list[tuple[str, ContingencyOptions]]:
        # Clear just the group flags, retaining shared choices such as evaluation.
        # Labels are part of output filenames used by downstream comparison tools.
        empty = replace(
            options,
            n1=False,
            n11=False,
            long_term_p2=False,
            extreme_double_bus=False,
            extreme_double_branch=False,
            extreme_bus_branch=False,
            extreme_p1_p7=False,
        )
        groups: list[tuple[str, ContingencyOptions]] = []
        if options.n1:
            groups.append(("P1,2,4,7", replace(empty, n1=True)))
        if options.n11:
            groups.append(("P1-7", replace(empty, n11=True)))
        if options.long_term_p2:
            groups.append(("LongTerm", replace(empty, long_term_p2=True)))
        if options.extreme_bus_branch:
            groups.append(("EE_BusBranch", replace(empty, extreme_bus_branch=True)))
        if options.extreme_double_branch:
            groups.append(("EE_DBL_Branch", replace(empty, extreme_double_branch=True)))
        if options.extreme_double_bus:
            groups.append(("EE_DBL_Bus", replace(empty, extreme_double_bus=True)))
        if options.extreme_p1_p7:
            groups.append(("EE_P1+P7", replace(empty, extreme_p1_p7=True)))
        return groups

    def _write_outputs(self, base: Path, analysis: AnalysisOptions) -> list[Path]:
        # Stage each file beside its destination, then replace the final name.
        # Publication is per file, not a transaction across all selected formats.
        outputs: list[Path] = []
        def publish(temporary: Path, destination: Path) -> None:
            if not temporary.is_file() or not temporary.stat().st_size:
                raise PowerWorldError(f"No usable output was written: {temporary}")
            temporary.replace(destination)

        token = uuid4().hex[:8]
        if analysis.create_pwb:
            pwb = base.with_name(base.name + ".PWB")
            pending = base.with_name(f"{base.name}.{token}.pending.PWB")
            self.client.save_case(pending, "PWB", True)
            publish(pending, pwb)
            outputs.append(pwb)
            self.log(f"PWB output written: {pwb}")
        if analysis.create_aux:
            # Contingencies and selected branch result fields share one AUX file;
            # the second write appends instead of overwriting the first section.
            aux = base.with_name(base.name + ".AUX")
            pending = base.with_name(f"{base.name}.{token}.pending.AUX")
            self.client.write_aux_file(pending, "", "contingency", False, "all")
            self.client.write_aux_file(pending, "", "branch", True, BRANCH_RESULT_FIELDS)
            publish(pending, aux)
            outputs.append(aux)
            self.log(f"AUX output written: {aux}")
        if analysis.create_csv:
            csv_path = base.with_name(base.name + ".CSV")
            pending = base.with_name(f"{base.name}.{token}.pending.CSV")
            self._write_csv(pending)
            publish(pending, csv_path)
            outputs.append(csv_path)
            self.log(f"CSV output written: {csv_path}")
        return outputs

    def _open_and_condition(self, case: Path, *, configure_workers: bool = True) -> None:
        # Manipulation files alter the base model; solve that model before adding
        # contingencies. Analysis defers pool setup until a recovery point exists.
        if not case.is_file():
            raise PowerWorldError(f"The case file does not exist: {case}")
        self.log(f"Opening case: {case}")
        self.client.open_case(case)
        try:
            self._apply_case_conditioning(case, configure_workers=configure_workers)
            try:
                self.client.run_script_command("SolvePowerFlow;")
            except PowerWorldError as initial_error:
                # The legacy manipulating AUX equivalences external areas. Some
                # cases need a looser tolerance, or AGC disabled, before the newly
                # equivalenced model reaches its first valid power-flow solution.
                self._recover_conditioned_power_flow(
                    case,
                    configure_workers=configure_workers,
                    initial_error=initial_error,
                )
        except Exception:
            self._close_safely()
            raise

    def _apply_case_conditioning(self, case: Path, *, configure_workers: bool) -> None:
        """Apply run configuration and the case's ordered manipulation inputs."""
        if self.settings.use_distributed and configure_workers:
            distributed_config = self.settings.distributed_config()
            mode = (
                "multiple computers"
                if self.settings.use_multiple_computers
                else "local computer only"
            )
            self.log(
                f"Distributed mode: {mode}; "
                f"{len(distributed_config.computers)} computer(s) configured for this run."
            )
            self._apply_distributed_config(distributed_config)
        for path in paths_for_case(self.settings.aux_files, MANUAL, str(case)):
            self._apply_file(path)

    def _power_flow_recovery_path(self) -> Path:
        """Return a unique writable PWB used only for solve recovery attempts."""
        from .settings import SettingsStore

        store = SettingsStore()
        store.base_dir.mkdir(parents=True, exist_ok=True)
        return store.base_dir / f"PowerFlowRecovery_{uuid4().hex[:12]}.pwb"

    def _recover_conditioned_power_flow(
        self,
        case: Path,
        *,
        configure_workers: bool,
        initial_error: PowerWorldError,
    ) -> None:
        """Retry a failed post-manipulation solve using the legacy recovery order.

        Each attempt reopens the same post-manipulation snapshot. This prevents a
        failed Newton solution from contaminating the next attempt. The first
        three retries relax convergence; the remaining four also disable area
        interchange control (AGC), matching the strategy in the original VB app.
        """
        self.log(
            "WARNING: The initial power flow did not solve after applying the "
            f"manipulation AUX file(s): {initial_error}"
        )
        self.log("Preparing bounded post-equivalence power-flow recovery attempts.")

        recovery_case = self._power_flow_recovery_path()
        last_error: Exception = initial_error
        solved = False
        try:
            # Rebuild from the original input because the failed solve may have
            # left controls in a partial state. Save the conditioned/equivalenced
            # model before changing tolerance or AGC for individual attempts.
            self._close_safely()
            self.client.open_case(case)
            self._apply_case_conditioning(case, configure_workers=configure_workers)
            self.client.save_case(recovery_case, "PWB", True)
            if not recovery_case.is_file() or recovery_case.stat().st_size == 0:
                raise PowerWorldError(
                    f"PowerWorld did not write the temporary recovery case: {recovery_case}"
                )

            attempts = (
                ("0.01", False),
                ("0.1", False),
                ("1.0", False),
                ("0.001", True),
                ("0.01", True),
                ("0.1", True),
                ("1.0", True),
            )
            for attempt_number, (tolerance, disable_agc) in enumerate(attempts, start=2):
                self._check_stop("post-equivalence power-flow recovery")
                if attempt_number > 2:
                    self._close_safely()
                    self.client.open_case(recovery_case)

                description = f"convergence tolerance={tolerance}"
                self.client.run_script_command(
                    f"SetData(Sim_Solution_Options, [ConvergenceTol], [{tolerance}]);"
                )
                if disable_agc:
                    self.client.run_script_command(
                        'SetData(Sim_Solution_Options, [ChkAreaInt], ["NO"]);'
                    )
                    description += ", AGC/area interchange disabled"
                self.log(f"Power-flow recovery attempt {attempt_number}/8: {description}.")
                try:
                    self.client.run_script_command("SolvePowerFlow;")
                except PowerWorldError as exc:
                    last_error = exc
                    self.log(f"Power-flow recovery attempt {attempt_number}/8 failed: {exc}")
                    continue

                solved = True
                self.log(f"Power flow solved on recovery attempt {attempt_number}/8: {description}.")
                break
        except StopRequested:
            raise
        except PowerWorldError as exc:
            last_error = exc

        if solved:
            try:
                recovery_case.unlink(missing_ok=True)
            except OSError as exc:
                self.log(f"Warning: temporary power-flow recovery case could not be removed: {exc}")
            return

        self.log(f"Power-flow recovery case retained for investigation: {recovery_case}")
        raise PowerWorldError(
            "The power flow did not solve after applying the manipulation AUX file(s), "
            "including seven bounded recovery attempts with relaxed convergence and AGC disabled. "
            f"Last PowerWorld error: {last_error}"
        ) from last_error

    def settings_store_aux_path(self) -> Path:
        from .settings import SettingsStore

        # Analysis gets a run-specific file; standalone preparation uses the
        # shared saved-configuration location. Neither writes into bundled Resources.
        return self._runtime_aux or SettingsStore().distributed_aux_path

    def _apply_distributed_config(self, config) -> None:
        config.validate()
        # AUX merges rows. Exclude stale machines without toggling selected ones.
        self.client.disable_distributed_computers_except([computer.name for computer in config.computers])
        self.log(f"Session {self.client.session_id}: configuring {len(config.computers)} computer(s), "
                 f"{sum(c.connections for c in config.computers)} requested workers, "
                 f"{config.contingencies_per_process} contingencies per process. "
                 "Stored authentication is unchanged.")
        path = self.settings_store_aux_path()
        write_distributed_aux(path, config)
        self._apply_file(path)

    def _temporary_contingency_aux_path(self) -> Path:
        from .settings import SettingsStore

        store = SettingsStore()
        store.base_dir.mkdir(parents=True, exist_ok=True)
        return store.base_dir / "P_All.aux"

    def build_contingencies(self, case: Path, options: ContingencyOptions) -> None:
        # Sequence is significant: form combinations before loading non-combinable
        # contingencies and operating guides. Reordering can change study counts.
        self.log("Clearing contingencies already stored in the case.")
        self.client.run_script_command('Delete(Contingency, "");')

        if options.n1 or options.n11 or options.extreme_p1_p7:
            self._apply_file(self.resources_dir / "P1_All_Except_CB-CB.aux")
            self._apply_category(case, GENERIC_CONTINGENCY)

        if options.evaluate_nonexistent and (options.n1 or options.n11):
            self._mark_nonexistent_contingencies_skipped()

        if options.n11:
            if options.use_model_build:
                self.log("Using the model-build N-1-1 path.")
                self._apply_file(self.resources_dir / "N-2_Branches&TFMRs.aux")
            else:
                self.log("Joining active N-1 contingencies to build N-1-1 contingencies.")
                self.client.run_script_command("CTGJoinActiveCTGs(NO,NO,YES);")

        # Both legacy P1,2,4,7 and P1-7 runs add the P2/non-combinable
        # stages after creating the base P1 contingencies.
        if options.n1 or options.n11:
            self._apply_file(self.resources_dir / "P2_All_Except_BF.aux")
            self._apply_category(case, JOIN_ACTIVE_CONTINGENCY)
            self._apply_category(case, GENERIC_CONTINGENCY_POST)
            self._apply_category(case, COMMON_STRUCTURE)

        if options.long_term_p2:
            # LongTerm combines P2 with underground-line outages. In the combined
            # preparation workflow, save other groups before clearing the CTG table
            # so they are restored afterward, not included in this cross-product.
            ug_file = self.resources_dir / "UG_Lines.aux"
            saved_contingencies: Path | None = None
            if options.n1 or options.n11:
                saved_contingencies = self._temporary_contingency_aux_path()
                if saved_contingencies.exists():
                    saved_contingencies.unlink()
                escaped_saved = str(saved_contingencies).replace('"', '""')
                self.log("Temporarily saving the existing N-1/N-1-1 contingency set.")
                self.client.run_script_command(
                    f'SaveData("{escaped_saved}", AUX, CONTINGENCY,'
                    '[CTGLabel],[CTGElement],,[],NO);'
                )

            self.client.run_script_command('Delete(Contingency, "");')
            self._apply_file(self.resources_dir / "P2_All_Except_BF.aux")
            self._apply_category(case, JOIN_ACTIVE_CONTINGENCY)
            escaped_ug = str(ug_file).replace('"', '""')
            self.client.run_script_command(
                f'CTGJoinActiveCTGs(NO,YES,NO,"{escaped_ug}");'
            )

            if saved_contingencies is not None:
                self.log("Reloading the saved N-1/N-1-1 contingency set.")
                self._apply_file(saved_contingencies)

        if options.extreme_bus_branch:
            # Extreme Events use their own join/filter ordering. Do not assume
            # these branches are interchangeable with the normal N-1-1 recipe.
            self._apply_file(self.resources_dir / "N-1_All.aux")
            self._apply_category(case, GENERIC_CONTINGENCY)
            self._apply_file(self.resources_dir / "Conts_Filters.aux")
            if options.evaluate_nonexistent:
                self._mark_nonexistent_contingencies_skipped()
            p2_buses = self.resources_dir / "P2_Buses.aux"
            escaped = str(p2_buses).replace('"', '""')
            self.client.run_script_command(
                f'CTGJoinActiveCTGs(NO,YES,NO,"{escaped}");'
            )
        if options.extreme_double_branch:
            self._apply_category(case, GENERIC_CONTINGENCY)
            self._apply_file(self.resources_dir / "Conts_Filters.aux")
            if options.evaluate_nonexistent:
                self._mark_nonexistent_contingencies_skipped()
            n1_all = self.resources_dir / "N-1_All.aux"
            escaped = str(n1_all).replace('"', '""')
            self.client.run_script_command(
                f'CTGJoinActiveCTGs(NO,YES,NO,"{escaped}");'
            )
            self._apply_file(self.resources_dir / "N-2_Branches&TFMRs.aux")
        if options.extreme_double_bus:
            self._apply_file(self.resources_dir / "Conts_Filters.aux")
            self._apply_file(self.resources_dir / "EE_DBL_Bus.aux")
        if options.extreme_p1_p7:
            self._apply_file(self.resources_dir / "Conts_Filters.aux")
            if options.evaluate_nonexistent:
                self._mark_nonexistent_contingencies_skipped()
            self._apply_category(case, COMMON_STRUCTURE, join_active=True)

        if options.n11:
            self._apply_category(case, OPERATING_GUIDE)
        if not any(
            (
                options.extreme_bus_branch,
                options.extreme_double_branch,
                options.extreme_double_bus,
                options.extreme_p1_p7,
            )
        ):
            self._apply_file(self.resources_dir / "Conts_Filters.aux")

    def _mark_nonexistent_contingencies_skipped(self) -> int:
        """Match the active part of the legacy removeDupConts routine.

        The duplicate-removal portion was commented out in VB. The working part
        marks a contingency skipped when PowerWorld cannot resolve its element.
        """
        self.log("Evaluating contingencies for elements that do not exist in the case.")
        result = self.client.get_parameters_multiple(
            "ContingencyElement", ("CTGLabel", "WhoAmI"), ""
        )
        try:
            columns = result[1]
            labels, identities = columns[0], columns[1]
        except (IndexError, TypeError, ValueError) as exc:
            raise PowerWorldError(
                "PowerWorld returned an unexpected contingency-element table."
            ) from exc

        skipped_labels: list[str] = []
        # One missing element skips its whole contingency. A label can have many
        # elements, so update it once; BASECASE is deliberately left alone.
        seen: set[str] = set()
        for label, identity in zip(labels, identities):
            clean_label = str(label).strip()
            if (
                clean_label
                and clean_label.casefold() != "basecase"
                and not str(identity or "").strip()
                and clean_label not in seen
            ):
                self.client.change_parameters_single(
                    "Contingency",
                    ("CTGLabel", "CTGSkip"),
                    (clean_label, "YES"),
                )
                seen.add(clean_label)
                skipped_labels.append(clean_label)

        self.log(
            f"Marked {len(skipped_labels)} non-existent contingenc"
            f"{'y' if len(skipped_labels) == 1 else 'ies'} as skipped."
        )
        return len(skipped_labels)

    def _configure_analysis(self, options: AnalysisOptions, *, force_ac: bool = False) -> None:
        # SimAuto's method values are 0=AC and 1=DC. Apply monitoring thresholds
        # for the actual stage, not just the main-window mode selected at startup.
        calculation_method = (
            "0" if options.full_ac or options.mode == "ac_screen" or force_ac else "1"
        )
        self.client.change_parameters_single(
            "Ctg_Options",
            ("CTG_CalculationMethod",),
            (calculation_method,),
        )
        monitor_percent = options.ac_monitor_percent if calculation_method == "0" else options.dc_monitor_percent
        self.client.change_parameters_single(
            "LimitSet",
            ("LSNum", "LSName", "LSLinePercent"),
            ("1", "Default", str(monitor_percent)),
        )
        self.client.change_parameters_single(
            "Ctg_Options",
            ("CTG_WhatToDoWithBC", "CTG_BCFlows", "CTG_BCLowVolt", "CTG_BCHighVolt"),
            # Voltage cutoffs are entered as percentages but passed as fractions.
            # Preserve the legacy flow cutoff and base-case re-report mode mapping.
            (
                "2" if options.do_not_rereport else "1",
                str(options.rereport_tdf_cutoff),
                str(options.rereport_volt_cutoff / 100),
                str(options.rereport_volt_cutoff / 100),
            ),
        )

    def _configure_n1_controls(self) -> None:
        """Match the three control options set by the legacy N-1 workflow."""
        self.log("Disabling shunt, tap, and LTC tap-balance checks for N-1 analysis.")
        for option in ("ChkShunts", "ChkTaps", "LTCTapBalance"):
            self.client.run_script_command(
                f'SetData(CTG_Options_Value, [Option, Value], ["{option}", "NO"]);'
            )

    def _verify_distributed_workers(self, stage: str) -> tuple[PowerWorldComputerStatus, ...]:
        # These are same-session readiness retries. StudyExecution owns the outer
        # fresh-session retries if this loop exhausts its bounded attempts.
        delays = (0.0,) + self.distributed_retry_delays
        last_error: Exception | None = None
        for attempt, delay in enumerate(delays, start=1):
            self._check_stop(stage)
            if delay:
                self.log(
                    f"No distributed workers were available for {stage}. "
                    f"Waiting {delay:g} seconds before verification attempt {attempt} of {len(delays)}."
                )
                self._wait_for_workers(delay, stage)

            self.log(
                f"Verifying distributed computers for {stage} "
                f"(attempt {attempt} of {len(delays)})."
            )
            try:
                if attempt > 1:
                    self._apply_distributed_config(self._active_distributed_config or self.settings.distributed_config())
                started = time.monotonic()
                self.client.run_script_command("VerifyDistributedComputersAvailable;")
                self._check_stop(stage)
                raw = self.client.get_parameters_multiple(
                    "DistributedComputer",
                    (
                        "DSServerName",
                        "Enabled:1",
                        "NumberOfConnections",
                        "NumberOfConnections:1",
                        "DistNumErrors",
                    ),
                    "",
                )
                configured = self._active_distributed_config or self.settings.distributed_config()
                # Match against the requested pool, not every row PowerWorld knows.
                # Missing rows remain in the denominator; stale extras cannot count
                # as capacity, and changed allocations are not silently accepted.
                returned = {status.name.casefold(): status for status in parse_powerworld_computer_statuses(raw)}
                statuses_list = []
                for computer in configured.computers:
                    status = returned.get(computer.name.strip().casefold())
                    if status is None:
                        self.log(f"  {computer.name}: MISSING from PowerWorld's returned worker table.")
                        status = PowerWorldComputerStatus(computer.name, False, computer.connections, 0, 0)
                    elif status.requested_workers != computer.connections:
                        raise PowerWorldError(
                            f"Worker allocation mismatch for {computer.name}: configured={computer.connections}, "
                            f"PowerWorld returned={status.requested_workers}."
                        )
                    statuses_list.append(status)
                statuses = tuple(statuses_list)
                self.log(f"Session {self.client.session_id}: verification took {time.monotonic() - started:.1f}s.")
            except (PowerWorldError, ValueError) as exc:
                last_error = exc
                self.log(f"Distributed verification attempt {attempt} failed: {exc}")
                empty_table = isinstance(exc, PowerWorldError) and "no data returned" in str(exc).casefold()
                if isinstance(exc, ValueError) or (not empty_table and not is_worker_failure(exc)):
                    raise PowerWorldError(f"Distributed verification failed: {exc}") from exc
                continue

            ready = tuple(status for status in statuses if status.available and status.requested_workers > 0)
            requested_total = sum(status.requested_workers for status in statuses)
            ready_workers = sum(status.requested_workers for status in ready)
            detected_total = sum(status.detected_cores for status in ready)
            for status in statuses:
                state = "READY" if status.available else "UNAVAILABLE"
                self.log(
                    f"  {status.name}: {state}; requested workers={status.requested_workers}; "
                    f"detected cores={status.detected_cores}; errors={status.errors}"
                )
            self.log(
                f"Distributed capacity for {stage}: {len(ready)} of {len(statuses)} computers, "
                f"{ready_workers} of {requested_total} requested workers, "
                f"{detected_total} detected cores available."
            )
            if ready:
                # Partial availability is allowed and logged. The solve path will
                # exclude unavailable hosts; zero availability triggers recovery.
                if len(ready) != len(statuses):
                    self.log("WARNING: Continuing with the distributed computers that are available.")
                self.progress({"stage": f"Workers ready for {stage}", "workers": ready_workers,
                               "computers": len(ready)})
                return statuses
            last_error = PowerWorldError(
                f"PowerWorld reported 0 of {len(statuses)} distributed computers available for {stage}."
            )

        detail = f": {last_error}" if last_error is not None else ""
        raise PowerWorldError(
            f"No distributed computers are available for {stage} after {len(delays)} attempts{detail}"
        )

    def _run_solve_all(
        self,
        *,
        distributed: bool,
        stage: str,
        preserve_results: bool = False,
    ) -> None:
        if distributed:
            verification_started = time.monotonic()
            statuses = self._verify_distributed_workers(stage)
            # Verification can enable saved/registry rows. Exclude only those
            # not selected/ready; do not disable and re-enable ready workers.
            ready = [status for status in statuses if status.available and status.requested_workers > 0]
            self.client.disable_distributed_computers_except([status.name for status in ready])
            self.log(f"Worker preparation for {stage}: {time.monotonic() - verification_started:.1f}s "
                     "(includes verification/retry waits; capacity is not measured CPU usage).")
            self.progress({"stage": stage, "workers": sum(s.requested_workers for s in ready),
                           "computers": len(ready)})
        else:
            self.progress({"stage": stage + " (local)"})
        if self.stop_requested():
            raise StopRequested(f"Stopped before starting {stage}.")
        suffix = ",NO" if preserve_results else ""
        # CTGSolveAll's second argument controls clearing results. Later Full AC
        # batches must pass NO or earlier batches' results can be erased.
        command = f"CTGSolveAll({'YES' if distributed else 'NO'}{suffix});"
        if distributed:
            self.log(f"Workers verified in session {self.client.session_id}. Requesting distributed solve for {stage}.")
        else:
            self.log(f"PowerWorld is now running {stage}.")
        started = time.monotonic()
        try:
            self.client.run_script_command(command)
        finally:
            self.log(f"PowerWorld solve call for {stage}: {time.monotonic() - started:.1f}s; command={command}")

    def _check_stop(self, stage: str) -> None:
        if self.stop_requested():
            raise StopRequested(f"Stopped while preparing {stage}.")

    def _wait_for_workers(self, seconds: float, stage: str) -> None:
        # Check cancellation between one-second slices. COM itself is blocking.
        remaining = seconds
        while remaining > 0:
            self._check_stop(stage)
            self.progress({"stage": f"Waiting for workers: {stage} ({remaining:.0f}s remaining)"})
            interval = min(1.0, remaining)
            self.wait(interval)
            remaining -= interval
        self._check_stop(stage)

    def _restart_from_checkpoint(self, checkpoint: Path, stage: str) -> None:
        """Release a failed/finished pool and reopen a known-good case state."""
        self.log(f"Recovering {stage}: closing coordinator session {self.client.session_id}.")
        self._close_safely()
        self.client.disconnect()

        if self.distributed_pool_release_delay > 0:
            self.log(
                f"Waiting {self.distributed_pool_release_delay:g} seconds for distributed "
                "workers to be released."
            )
            self._wait_for_workers(self.distributed_pool_release_delay, stage)

        self.log(f"Starting a fresh SimAuto session for {stage}.")
        self.client = self.client_factory()
        self.log(f"New recovery coordinator session {self.client.session_id}.")
        self.client.open_case(checkpoint)
        self.log(f"Recovery checkpoint opened for {stage}: {checkpoint}")

    def _apply_category(self, case: Path, category: str, *, join_active: bool = False) -> None:
        # Applying a file and joining against that file are different operations.
        # EE P1+P7 needs the latter, using only this case's selected structure files.
        for path in paths_for_case(self.settings.aux_files, category, str(case)):
            if join_active:
                escaped = str(path).replace('"', '""')
                self.client.run_script_command(f'CTGJoinActiveCTGs(NO,YES,NO,"{escaped}");')
            else:
                self._apply_file(path)

    def _apply_file(self, path: str | Path) -> None:
        self.log(f"Applying file: {path}")
        apply_input_file(self.client, path)

    def _write_csv(self, path: Path) -> None:
        result = self.client.get_parameters_multiple("Contingency", CSV_RESULT_FIELDS, "")
        try:
            columns = result[1]
            rows = zip(*columns)
        except (IndexError, TypeError) as exc:
            raise PowerWorldError("PowerWorld returned an unexpected contingency result table.") from exc
        with path.open("w", newline="", encoding="utf-8-sig") as stream:
            # UTF-8 with a BOM helps Windows spreadsheet tools recognize labels;
            # csv.writer handles commas/quotes without hand-built string escaping.
            writer = csv.writer(stream)
            writer.writerow(CSV_RESULT_FIELDS)
            writer.writerows(rows)

    def _log_contingency_count(self) -> int:
        result = self.client.get_parameters_multiple("Contingency", ("CTGLabel",), "")
        try:
            count = len(result[1][0])
        except (IndexError, TypeError) as exc:
            raise PowerWorldError("PowerWorld returned an unexpected contingency list.") from exc
        self.log(f"Contingencies currently loaded: {count}")
        return count

    def _close_safely(self) -> None:
        try:
            self.client.close_case()
        except PowerWorldError as exc:
            self.log(f"Warning: {exc}")
