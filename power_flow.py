"""Prepare a solved base case without carrying temporary recovery settings onward.

PowerWorld's legacy ConvergenceTol field is per unit. Keep its original value
for restoration, but describe attempts in MVA using the case's actual SBase.
These checks establish a starting solution; they cannot prove study-result parity.
"""

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .powerworld import PowerWorldError


@dataclass(frozen=True)
class SolutionSettings:
    tolerance: str
    base_mva: float
    agc: str
    dc: str

    @property
    def tolerance_mva(self) -> float:
        return float(self.tolerance) * self.base_mva

    @classmethod
    def read(cls, client):
        result = client.get_parameters_multiple(
            "Sim_Solution_Options", ("ConvergenceTol", "SBase", "ChkAreaInt", "DCPFMode"), ""
        )
        try:
            columns = result[1]
            if len(columns) != 4 or any(len(column) != 1 for column in columns):
                raise ValueError("Expected one solution-options record")
            tolerance, base, agc, dc = (str(column[0]).strip() for column in columns)
            settings = cls(tolerance, float(base), agc.upper(), dc.upper())
            if (not math.isfinite(settings.base_mva) or settings.base_mva <= 0
                    or not math.isfinite(settings.tolerance_mva) or settings.tolerance_mva <= 0
                    or settings.agc not in {"YES", "NO"} or settings.dc not in {"YES", "NO"}):
                raise ValueError("Invalid tolerance, MVA base, AGC, or DC flag")
            return settings
        except (IndexError, TypeError, ValueError, OverflowError) as exc:
            raise PowerWorldError(f"Cannot read the intended power-flow settings: {exc}") from exc

    def apply(self, client, *, tolerance_mva=None, agc=None):
        # Always key writes to the singleton solution-options object. Never use
        # guessed defaults: the user's AUX may intentionally choose other values.
        tolerance = self.tolerance if tolerance_mva is None else format(tolerance_mva / self.base_mva, ".15g")
        client.change_parameters_single(
            "Sim_Solution_Options", ("ConvergenceTol", "ChkAreaInt", "DCPFMode"),
            (tolerance, self.agc if agc is None else agc, self.dc),
        )


def save_solver_log(client, case: Path, log: Callable[[str], None]) -> None:
    """Keep PowerWorld's own messages alongside a failed preparation snapshot."""
    destination = case.with_suffix(".powerworld.log.txt")
    escaped = str(destination).replace('"', '""')
    try:
        client.run_script_command(f'LogSave("{escaped}", NO);')
        log(f"PowerWorld diagnostic log requested at: {destination}")
    except PowerWorldError as exc:
        log(f"Could not save the PowerWorld diagnostic log: {exc}")


def solve_conditioned_case(client, snapshot: Path, *, log, check_stop) -> None:
    """Try clean starts; accept recovery only after the original settings solve.

    Save BEFORE the first attempt. Reapplying an equivalencing AUX after a failed
    solve can produce a different model and repeats its external-file side effects.
    The saved snapshot also makes every fallback independent of a failed iteration.
    """
    target = SolutionSettings.read(client)
    if target.dc == "YES":
        raise PowerWorldError(
            "The manipulated case has DC approximation enabled. An AC base solution is required "
            "before this study; check DCPFMode/DCApprox in the input case and manipulation AUX."
        )
    log(f"Post-manipulation target: {target.tolerance_mva:g} MVA convergence "
        f"({target.tolerance} pu on {target.base_mva:g} MVA base); AGC={target.agc}.")
    client.save_case(snapshot, "PWB", True)
    if not snapshot.is_file() or snapshot.stat().st_size == 0:
        raise PowerWorldError(f"PowerWorld did not write the preparation snapshot: {snapshot}")

    # Alternate Newton formulation first. Further attempts are starting aids,
    # never permission to deliver results at looser tolerances or with AGC off.
    attempts = [("SolvePowerFlow;", target.tolerance_mva, target.agc)]
    attempts.append(("SolvePowerFlow(POLARNEWTON);", target.tolerance_mva, target.agc))
    if target.agc == "YES":
        attempts.append(("SolvePowerFlow;", target.tolerance_mva, "NO"))
    for mva in (1.0, 10.0):
        if mva > target.tolerance_mva:
            attempts.append(("SolvePowerFlow;", mva, target.agc))
            if target.agc == "YES":
                attempts.append(("SolvePowerFlow;", mva, "NO"))

    accepted = False
    last_error = None
    try:
        for number, (command, mva, agc) in enumerate(attempts, 1):
            check_stop("post-manipulation power-flow preparation")
            if number > 1:
                client.close_case()
                client.open_case(snapshot)
                target.apply(client, tolerance_mva=mva, agc=agc)
            log(f"Power-flow preparation attempt {number}/{len(attempts)}: "
                f"{mva:g} MVA; AGC={agc}; {command}")
            try:
                client.run_script_command(command)
            except PowerWorldError as exc:
                # Only numerical failures justify a different starting method.
                # COM, license and invalid-option failures must remain visible.
                message = str(exc).casefold()
                if not any(word in message for word in ("converg", "power flow did not solve", "powerflow did not solve")):
                    raise
                last_error = exc
                log(f"Power-flow preparation attempt {number} failed: {exc}")
                continue

            if number > 1:
                target.apply(client)
                log(f"Rechecking recovered solution at the required {target.tolerance_mva:g} MVA "
                    f"and original AGC={target.agc}.")
                try:
                    client.run_script_command("SolvePowerFlow;")
                except PowerWorldError as exc:
                    if "converg" not in str(exc).casefold() and "did not solve" not in str(exc).casefold():
                        raise
                    last_error = exc
                    log(f"Recovery candidate rejected at the original settings: {exc}")
                    continue
            # Do not trust a successful call if a script/solution action has
            # silently changed the options that the caller asked us to use.
            actual = SolutionSettings.read(client)
            if (not math.isclose(actual.tolerance_mva, target.tolerance_mva, rel_tol=1e-9)
                    or actual.agc != target.agc or actual.dc != target.dc):
                raise PowerWorldError("Power-flow settings changed during preparation validation.")
            accepted = True
            log(f"Conditioned AC base case verified at {target.tolerance_mva:g} MVA; AGC={target.agc}.")
            return

        raise PowerWorldError(
            "The manipulated case could not solve at its intended power-flow settings. "
            "Temporary recovery settings were not accepted as a study solution. "
            f"No contingency study was started. Preparation snapshot: {snapshot}. "
            f"Last PowerWorld error: {last_error}"
        ) from last_error
    finally:
        if accepted:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError as exc:
                log(f"Could not remove the completed preparation snapshot: {exc}")
        else:
            log(f"Unsolved preparation snapshot retained: {snapshot}")
            save_solver_log(client, snapshot, log)


def check_contingency_reference(client, *, snapshot_path, log, check_stop) -> None:
    """Solve one empty AC contingency using the study's actual CTG options.

    This is separate from the optional violation check. It tests convergence without
    an outage after all AUX files/overrides have loaded, and costs one local solve.
    No island, slack, monitoring, or contingency-specific options are relaxed.
    """
    check_stop("AC reference check")
    label = f"__TPL_REFERENCE_CHECK_{uuid4().hex}"
    device_filter = f"<DEVICE>Contingency '{label}'"
    client.run_script_command("CTGSetAsReference;")
    created = False
    failed = False
    try:
        client.run_script_command(f'CreateData(Contingency, [CTGLabel, CTGSkip], ["{label}", "NO"]);')
        created = True
        client.run_script_command("EnterMode(Run);")
        log("Checking the AC contingency reference with no outage before starting the study.")
        client.run_script_command(f'CTGSolve("{label}");')
        result = client.get_parameters_multiple("Contingency", ("CTGLabel", "CTGSolved"), device_filter)
        try:
            labels, statuses = result[1]
            if list(labels) != [label] or len(statuses) != 1:
                raise ValueError("Missing or ambiguous reference-check result")
            status = str(statuses[0]).strip()
        except (IndexError, TypeError, ValueError) as exc:
            raise PowerWorldError(f"Cannot validate the AC reference check: {exc}") from exc
        if status.upper() not in {"YES", "TRUE", "1"}:
            raise PowerWorldError(f"The no-outage AC contingency did not solve. PowerWorld status: {status!r}")
        log("No-outage AC contingency reference check passed.")
    except PowerWorldError as exc:
        failed = True
        # Keep the failing probe and its status in a separate diagnostic case.
        # Do not let a diagnostic-save failure replace the original explanation.
        try:
            snapshot = snapshot_path()
            client.change_parameters_single(
                "Ctg_Options", ("CTGSaveInPWB", "CTG_AlwaysSaveResults"), ("YES", "YES")
            )
            client.save_case(snapshot, "PWB", True)
            log(f"AC reference-check diagnostic case: {snapshot}")
            save_solver_log(client, snapshot, log)
        except (OSError, PowerWorldError) as save_error:
            log(f"Could not save the AC reference-check case: {save_error}")
        raise PowerWorldError(
            f"AC reference check failed before the contingency study: {exc}. "
            "Check the manipulation AUX and contingency solution/make-up-power options."
        ) from exc
    finally:
        # CTGSolve leaves a post-contingency state. Restore even for an empty
        # contingency: make-up-power controls may have moved generation.
        cleanup_errors = []
        commands = ["CTGRestoreReference;"]
        if created:
            commands.append(f'Delete(Contingency, "{device_filter}");')
        for command in commands:
            try:
                client.run_script_command(command)
            except PowerWorldError as exc:
                cleanup_errors.append(str(exc))
        if cleanup_errors:
            message = "AC reference-check cleanup failed: " + "; ".join(cleanup_errors)
            if failed:
                log(message)
            else:
                # Continuing with a changed base state or a leftover probe would
                # contaminate the real study; cleanup is part of a passed check.
                raise PowerWorldError(message)
