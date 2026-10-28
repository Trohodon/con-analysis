from __future__ import annotations

# Main UI and job coordinator. Widget callbacks gather choices; core/ owns study
# rules. Analysis/resume jobs run off-thread and communicate through _task_queue.
# The synchronous preparation buttons are separate from that long-run path.

import queue
from copy import deepcopy
import time
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from core.analysis import AnalysisOptions, ContingencyOptions, ContingencyWorkflow
from core.aux_files import MANUAL, paths_for_case
from core.powerworld import PowerWorldClient, PowerWorldError, launch_powerworld
from core.preparation import default_prepared_case_path, prepare_case
from core.session_log import CaseSessionLog
from core.settings import SettingsStore
from core.recovery import StopRequested
from .aux_dialog import AuxSelectionDialog
from .extreme_events_dialog import ExtremeEventsDialog
from .worker_monitor import WorkerMonitor

APP_TITLE = "NERC TPL CA - Dominion Energy"
APP_SUBTITLE = "PowerWorld contingency-analysis setup"
APP_VERSION = "2026.10.28"
# Match the team's desktop-tool palette; assets resolve in source or the bundled
# PyInstaller layout because they are packaged alongside the application modules.
NAVY = "#0B2F5B"
LIGHT_BLUE = "#DCE7F5"
APP_ICON = Path(__file__).resolve().parent.parent / "assets" / "app.ico"


class App(ttk.Frame):
    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent)
        self.parent = parent
        self.store = SettingsStore()
        self.settings = self.store.load()
        self.dist_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready")
        self._task_queue: queue.Queue[tuple] = queue.Queue()
        # Tk widgets stay on this thread. Worker messages carry plain data, and
        # the stop Event is the thread-safe signal for cooperative cancellation.
        self._task_running = False
        self._stop_event = threading.Event()
        self._task_started = 0.0
        self._stage_started = 0.0
        self._stage_name = "Ready"
        self._run_detail = ""
        self._worker_monitor: WorkerMonitor | None = None
        self.pack(fill="both", expand=True)
        self._configure_window()
        self._configure_style()
        self._build_menu()
        self._build_header()
        self._build_body()
        self._build_status_bar()
        self.parent.after(100, self._poll_task_queue)
        self.parent.after(1000, self._tick_run_clock)
        self.parent.protocol("WM_DELETE_WINDOW", self._close_window)

    def _configure_window(self) -> None:
        self.parent.title(APP_TITLE)
        try:
            self.parent.iconbitmap(default=str(APP_ICON))
        except tk.TclError:
            # Keep the application usable if a development copy is missing
            # the icon. Packaged builds include the complete assets folder.
            pass
        screen_width = self.parent.winfo_screenwidth()
        # A tall, roughly half-screen window keeps the legacy horizontal sections
        # together while leaving room beside the app for PowerWorld or the log.
        screen_height = self.parent.winfo_screenheight()
        window_width = max(940, int(screen_width * 0.52))
        window_height = max(760, int(screen_height * 0.90))
        window_width = min(window_width, screen_width - 40)
        window_height = min(window_height, screen_height - 80)
        left = max(20, (screen_width - window_width) // 2)
        top = max(20, (screen_height - window_height) // 2)
        self.parent.geometry(f"{window_width}x{window_height}+{left}+{top}")
        self.parent.minsize(900, 700)

    @staticmethod
    def _configure_style() -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TButton", padding=(10, 5))
        style.configure("TEntry", padding=(5, 3))
        style.configure("TCombobox", padding=(5, 3))
        style.configure("TNotebook.Tab", padding=(12, 8))
        style.configure("Card.TLabelframe", padding=(10, 7))
        style.configure("Card.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 9))
        style.configure("Go.TButton", font=("Segoe UI", 11, "bold"), padding=(20, 8))

    def _build_menu(self) -> None:
        menu = tk.Menu(self.parent)
        run_menu = tk.Menu(menu, tearoff=False)
        run_menu.add_command(label="Resume saved group...", command=self._resume_analysis)
        menu.add_cascade(label="Run", menu=run_menu)
        view_menu = tk.Menu(menu, tearoff=False)
        view_menu.add_command(label="Distributed Worker Monitor", command=self._open_worker_monitor)
        menu.add_cascade(label="View", menu=view_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="About", command=self._show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.parent.configure(menu=menu)

    def _open_worker_monitor(self) -> None:
        if self._worker_monitor is not None and self._worker_monitor.winfo_exists():
            self._worker_monitor.deiconify()
            self._worker_monitor.lift()
            self._worker_monitor.focus_force()
            return
        self._worker_monitor = WorkerMonitor(self.parent, self.settings)

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=NAVY)
        header.pack(fill="x")
        tk.Label(header, text=APP_TITLE, bg=NAVY, fg="white", font=("Segoe UI", 16, "bold"), padx=14, pady=10).pack(side="left")
        tk.Label(header, text=APP_SUBTITLE, bg=NAVY, fg=LIGHT_BLUE, font=("Segoe UI", 10), padx=10).pack(side="left")
        tk.Label(header, text=APP_VERSION, bg=NAVY, fg=LIGHT_BLUE, font=("Segoe UI", 10, "bold"), padx=14).pack(side="right")

    def _build_body(self) -> None:
        # Keep the setup-to-run reading order familiar to legacy users. Most cards
        # stay compact; the log below them takes remaining vertical space.
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        self._build_case_setup(body)
        self._build_contingencies(body)
        self._build_method(body)
        self._build_analysis_options(body)
        self._build_actions(body)
        self._build_output_options(body)
        self._build_log(body)

    def _build_case_setup(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x")
        version = ttk.LabelFrame(row, text="Simulator Version", style="Card.TLabelframe")
        version.pack(side="left", fill="y", padx=(0, 8))
        self.version_var = tk.StringVar(value=str(self.settings.simulator_version))
        selector = ttk.Combobox(version, textvariable=self.version_var, values=("21", "22", "23", "24", "25"), width=8, state="readonly")
        selector.pack(side="left", anchor="n")
        selector.bind("<<ComboboxSelected>>", self._version_changed)
        self.version_label_var = tk.StringVar(value=f"Version {self.version_var.get()} selected")
        ttk.Label(version, textvariable=self.version_label_var).pack(side="left", anchor="n", padx=(8, 0), pady=4)

        cases = ttk.LabelFrame(row, text="RAW or PWB file to open", style="Card.TLabelframe")
        cases.pack(side="left", fill="both", expand=True)
        buttons = ttk.Frame(cases)
        buttons.pack(fill="x", pady=(0, 6))
        ttk.Button(buttons, text="Select Case(s)...", command=self._add_cases).pack(side="left")
        ttk.Button(buttons, text="Remove", command=self._remove_cases).pack(side="left", padx=6)
        ttk.Button(
            buttons,
            text="Open file in PowerWorld",
            command=self._open_selected_case,
        ).pack(side="right")
        list_frame = ttk.Frame(cases)
        list_frame.pack(fill="both", expand=True)
        self.case_list = tk.Listbox(list_frame, height=2, selectmode="extended", font=("Segoe UI", 9))
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.case_list.yview)
        self.case_list.configure(yscrollcommand=scroll.set)
        self.case_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_contingencies(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Contingencies", style="Card.TLabelframe")
        frame.pack(fill="x", pady=(8, 0))
        self.n1_var = tk.BooleanVar(value=True)
        self.n11_var = tk.BooleanVar(value=True)
        self.long_term_var = tk.BooleanVar(value=True)
        self.extreme_var = tk.BooleanVar(value=False)
        ttk.Button(frame, text="Select AUX Files...", command=self._open_aux).grid(row=0, column=0, sticky="w", padx=(0, 18))
        ttk.Checkbutton(frame, text="N-1 CA (P1, 2, 4-5, 7)", variable=self.n1_var).grid(row=0, column=1, sticky="w", padx=(0, 18))
        ttk.Checkbutton(frame, text="N-1-1 CA (P3, 6)", variable=self.n11_var).grid(row=0, column=2, sticky="w", padx=(0, 18))
        ttk.Checkbutton(
            frame,
            text="Long-term outage elements (UG Lines) + P2",
            variable=self.long_term_var,
        ).grid(row=0, column=3, sticky="w", padx=(0, 18))
        ttk.Checkbutton(frame, text="Extreme Events", variable=self.extreme_var).grid(row=0, column=4, sticky="w")
        ttk.Button(frame, text="Select EEs...", command=self._open_extreme_events).grid(row=0, column=5, sticky="w", padx=(8, 0))
        frame.columnconfigure(5, weight=1)
        progress = ttk.Frame(frame)
        progress.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(10, 0))
        self.run_info_var = tk.StringVar(value="Ready")
        ttk.Label(progress, textvariable=self.run_info_var, wraplength=700).pack(fill="x")
        self.run_progress = ttk.Progressbar(progress, mode="indeterminate")
        # Activity only, not percent complete: the blocking SimAuto solve does not
        # provide a per-contingency progress stream through this interface.
        self.run_progress.pack(side="left", fill="x", expand=True, pady=(5, 0))
        self.stop_button = ttk.Button(progress, text="Stop safely", command=self._request_stop, state="disabled")
        self.stop_button.pack(side="right", padx=(10, 0))

    def _build_method(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Method", style="Card.TLabelframe")
        frame.pack(fill="x", pady=(8, 0))
        # Join Active is the validated release workflow. The legacy Model Build
        # routine is not exposed until its per-N-1 case loop is ported and live-tested.
        self.method_var = tk.StringVar(value="join")
        self.settings.use_model_build = False
        ttk.Radiobutton(
            frame,
            text="Use Join Active method",
            variable=self.method_var,
            value="join",
            command=self._method_changed,
        ).grid(row=0, column=0, sticky="w", padx=(0, 28))
        self.model_method_radio = ttk.Radiobutton(
            frame,
            text="Use model build method (not available in this test release)",
            variable=self.method_var,
            value="model",
            command=self._method_changed,
        )
        self.model_method_radio.grid(row=0, column=1, sticky="w", padx=(0, 28))
        self.model_method_radio.state(("disabled",))
        self.write_n1_var = tk.BooleanVar(value=False)
        self.write_n1_check = ttk.Checkbutton(
            frame,
            text="Create (write) N-1 models",
            variable=self.write_n1_var,
        )
        self.write_n1_check.grid(row=0, column=2, sticky="w", padx=(0, 28))
        self.evaluate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Evaluate to find/remove non-existent contingencies", variable=self.evaluate_var).grid(row=0, column=3, sticky="w")
        frame.columnconfigure(3, weight=1)
        self._method_changed(save=False)

    def _method_changed(self, save: bool = True) -> None:
        using_model_build = self.method_var.get() == "model"
        if using_model_build:
            self.write_n1_check.state(("!disabled",))
        else:
            self.write_n1_var.set(False)
            self.write_n1_check.state(("disabled",))
        if save:
            self.settings.use_model_build = using_model_build
            self.store.save(self.settings)

    def _build_analysis_options(self, parent: ttk.Frame) -> None:
        # StringVars allow users to type intermediate numeric text. Convert and
        # validate when starting a job, not on every keystroke.
        frame = ttk.LabelFrame(parent, text="Analysis Options", style="Card.TLabelframe")
        frame.pack(fill="x", pady=(8, 0))
        self.analysis_mode_var = tk.StringVar(value="dc_ac")
        self.full_ac_var = tk.BooleanVar(value=False)
        self.no_rereport_var = tk.BooleanVar(value=True)
        self.dc_limit_var = tk.StringVar(value=str(self.settings.dc_monitor_percent))
        self.ac_limit_var = tk.StringVar(value=str(self.settings.ac_monitor_percent))
        self.tdf_var = tk.StringVar(value="0.5")
        self.volt_var = tk.StringVar(value="0.5")
        self.screen_tdf_var = tk.StringVar(value="2")
        self.screen_volt_var = tk.StringVar(value="2.5")
        self.conts_per_file_var = tk.StringVar(value=str(self.settings.contingencies_per_file))
        ttk.Radiobutton(
            frame,
            text="Use DC with AC verification",
            variable=self.analysis_mode_var,
            value="dc_ac",
            command=self._analysis_mode_changed,
        ).grid(row=0, column=0, sticky="w")
        self.dc_limit_controls = self._labeled_entry(frame, "DC CA Lim Mon %", self.dc_limit_var, 0, 1)
        self.ac_limit_controls = self._labeled_entry(frame, "AC CA Lim Mon %", self.ac_limit_var, 0, 3)
        ttk.Checkbutton(frame, text="Do not re-report base-case violations with less than the cutoffs", variable=self.no_rereport_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._labeled_entry(frame, "TDF % cutoff", self.tdf_var, 1, 2)
        self._labeled_entry(frame, "Volt % cutoff", self.volt_var, 1, 4)
        self.ac_screen_radio = ttk.Radiobutton(
            frame,
            text="Use AC Screening (not available in this test release)",
            variable=self.analysis_mode_var,
            value="ac_screen",
            command=self._analysis_mode_changed,
        )
        self.ac_screen_radio.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.ac_screen_radio.state(("disabled",))
        self.screen_tdf_controls = self._labeled_entry(frame, "Screen TDF cutoff", self.screen_tdf_var, 2, 1)
        self.screen_volt_controls = self._labeled_entry(frame, "Screen Volt cutoff", self.screen_volt_var, 2, 3)
        ttk.Checkbutton(
            frame,
            text="Perform FULL AC CA",
            variable=self.full_ac_var,
            command=self._full_ac_changed,
        ).grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.conts_per_file_controls = self._labeled_entry(frame, "Contingencies/file", self.conts_per_file_var, 3, 1)
        self._analysis_mode_changed()
        self._full_ac_changed()

    @staticmethod
    def _labeled_entry(
        parent: ttk.Frame,
        text: str,
        variable: tk.StringVar,
        row: int,
        column: int,
    ) -> tuple[ttk.Label, ttk.Entry]:
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=column, columnspan=2, sticky="w", padx=(16, 8), pady=2)
        label = ttk.Label(holder, text=text)
        label.pack(side="left")
        entry = ttk.Entry(holder, textvariable=variable, width=8)
        entry.pack(side="left", padx=(6, 0))
        return label, entry

    @staticmethod
    def _set_controls_enabled(controls: tuple[ttk.Label, ttk.Entry], enabled: bool) -> None:
        state = "!disabled" if enabled else "disabled"
        for control in controls:
            control.state((state,))

    def _analysis_mode_changed(self) -> None:
        # Disable fields that do not apply so visible values are not mistaken for
        # active settings. Core validation also rejects unsupported run modes.
        dc_with_ac = self.analysis_mode_var.get() == "dc_ac"
        self._set_controls_enabled(self.dc_limit_controls, dc_with_ac)
        self._set_controls_enabled(self.ac_limit_controls, dc_with_ac)
        self._set_controls_enabled(self.screen_tdf_controls, not dc_with_ac)
        self._set_controls_enabled(self.screen_volt_controls, not dc_with_ac)

    def _full_ac_changed(self) -> None:
        self._set_controls_enabled(self.conts_per_file_controls, self.full_ac_var.get())

    def _build_actions(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Case Preparation", style="Card.TLabelframe")
        frame.pack(fill="x", pady=(8, 0))
        actions = (
            ("Apply Man AUX files, then open the case in PowerWorld", self._prepare_manual_case),
            (
                "Apply Man and Con AUX files, then open the case in PowerWorld",
                self._prepare_contingency_case,
            ),
            (
                "Apply Man and Con AUX files, then create PTI .con file",
                self._create_pti_file,
            ),
        )
        for column, (label, command) in enumerate(actions):
            ttk.Button(frame, text=label, command=command).grid(row=0, column=column, sticky="ew", padx=3)
            frame.columnconfigure(column, weight=1)

    def _build_output_options(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Run and Output", style="Card.TLabelframe")
        frame.pack(fill="x", pady=(8, 0))
        self.create_aux_var = tk.BooleanVar(value=False)
        self.create_pwb_var = tk.BooleanVar(value=True)
        self.create_csv_var = tk.BooleanVar(value=False)
        self.delete_skipped_var = tk.BooleanVar(value=True)
        self.basecase_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Use Distributed Computing", variable=self.dist_var).grid(row=0, column=0, sticky="w", padx=(0, 18))
        ttk.Checkbutton(frame, text="Create CA AUX", variable=self.create_aux_var).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(frame, text="Create CA PWB", variable=self.create_pwb_var).grid(row=0, column=2, sticky="w", padx=12)
        ttk.Checkbutton(frame, text="Create CA CSV", variable=self.create_csv_var).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(frame, text="Delete skipped conts when complete", variable=self.delete_skipped_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(frame, text="Check for base-case violations prior", variable=self.basecase_var).grid(row=1, column=2, columnspan=2, sticky="w", pady=(4, 0))
        self.run_button = ttk.Button(frame, text="Go!", style="Go.TButton", command=self._run_analysis)
        self.run_button.grid(row=0, column=5, rowspan=2, sticky="nsew", padx=(18, 0))
        frame.columnconfigure(5, weight=1)

    def _build_log(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Log", style="Card.TLabelframe")
        frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(frame, wrap="word", state="disabled", height=8, font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._write_log(
            "Ready for PowerWorld testing. Join Active, DC with AC verification, Full AC, "
            "distributed computing, and PWB/AUX/CSV outputs are connected."
        )

    def _build_status_bar(self) -> None:
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        bar = ttk.Frame(self, padding=(10, 6))
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(bar, text="PowerWorld is required only when analysis runs", style="Status.TLabel").pack(side="right")

    def _case_paths(self) -> list[str]:
        return list(self.case_list.get(0, "end"))

    def _selected_case(self) -> str | None:
        # Preparation/open actions operate on one case; Go! uses the entire list.
        selected = self.case_list.curselection()
        if selected:
            return str(self.case_list.get(selected[0]))
        cases = self._case_paths()
        if len(cases) == 1:
            return cases[0]
        messagebox.showinfo(
            "Select a case",
            "Select one RAW or PWB case from the case list first.",
            parent=self.parent,
        )
        return None

    def _open_selected_case(self) -> None:
        case = self._selected_case()
        if case is None:
            return
        try:
            launch_powerworld(case, int(self.version_var.get()))
        except (PowerWorldError, ValueError) as exc:
            messagebox.showerror("Open in PowerWorld", str(exc), parent=self.parent)
            self._write_log(f"ERROR: {exc}")
            return
        self._set_status("Opening selected case in PowerWorld")
        self._write_log(f"Opening in PowerWorld: {case}")

    def _prepare_manual_case(self) -> None:
        if self._busy():
            return
        case = self._selected_case()
        if case is None:
            return
        suggested = default_prepared_case_path(case, "_withAUXapplied")
        output = filedialog.asksaveasfilename(
            parent=self.parent,
            title="Save the prepared PowerWorld case",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=".pwb",
            filetypes=(("PowerWorld binary", "*.pwb"),),
        )
        if not output:
            return

        files: list[str] = []
        if self.dist_var.get() and self.store.distributed_aux_path.is_file():
            files.append(str(self.store.distributed_aux_path))
        files.extend(paths_for_case(self.settings.aux_files, MANUAL, case))
        if not files and not messagebox.askyesno(
            "No manipulating AUX files",
            "No manipulating AUX files apply to this case. Save a copy without applying AUX files?",
            parent=self.parent,
        ):
            return

        self._set_status("Preparing case with manipulating AUX files...")
        try:
            prepared = prepare_case(
                PowerWorldClient(),
                case,
                output,
                files,
                log=self._write_log,
            )
            launch_powerworld(prepared, int(self.version_var.get()))
        except (OSError, PowerWorldError, ValueError) as exc:
            messagebox.showerror("Prepare PowerWorld case", str(exc), parent=self.parent)
            self._set_status("Case preparation failed")
            self._write_log(f"ERROR: {exc}")
            return
        self._set_status("Prepared case saved and opened in PowerWorld")
        self._write_log(f"Prepared case complete: {prepared}")

    def _contingency_options(self) -> ContingencyOptions:
        # Saved EE selections only participate when the main EE checkbox is on.
        use_extreme = self.extreme_var.get()
        options = ContingencyOptions(
            n1=self.n1_var.get(),
            n11=self.n11_var.get(),
            long_term_p2=self.long_term_var.get(),
            extreme_double_bus=use_extreme and self.settings.extreme_double_bus,
            extreme_double_branch=use_extreme and self.settings.extreme_double_branch,
            extreme_bus_branch=use_extreme and self.settings.extreme_bus_branch,
            extreme_p1_p7=use_extreme and self.settings.extreme_p1_p7,
            use_model_build=self.method_var.get() == "model",
            write_n1_models=self.write_n1_var.get(),
            evaluate_nonexistent=self.evaluate_var.get(),
        )
        if use_extreme and not any(
            (
                options.extreme_double_bus,
                options.extreme_double_branch,
                options.extreme_bus_branch,
                options.extreme_p1_p7,
            )
        ):
            raise ValueError("Extreme Events is checked, but no extreme-event groups are selected.")
        options.validate()
        return options

    def _analysis_options(self) -> AnalysisOptions:
        # Read Tk variables before handing work to a background thread. The core
        # receives ordinary dataclasses, never Tk variables or entry widgets.
        try:
            options = AnalysisOptions(
                mode=self.analysis_mode_var.get(),
                dc_monitor_percent=int(self.dc_limit_var.get()),
                ac_monitor_percent=int(self.ac_limit_var.get()),
                screen_tdf_cutoff=float(self.screen_tdf_var.get()),
                screen_volt_cutoff=float(self.screen_volt_var.get()),
                do_not_rereport=self.no_rereport_var.get(),
                rereport_tdf_cutoff=float(self.tdf_var.get()),
                rereport_volt_cutoff=float(self.volt_var.get()),
                check_basecase=self.basecase_var.get(),
                full_ac=self.full_ac_var.get(),
                contingencies_per_file=int(self.conts_per_file_var.get()),
                use_distributed=self.dist_var.get(),
                delete_skipped=self.delete_skipped_var.get(),
                create_pwb=self.create_pwb_var.get(),
                create_aux=self.create_aux_var.get(),
                create_csv=self.create_csv_var.get(),
            )
        except ValueError as exc:
            raise ValueError("One of the numeric analysis settings is not a valid number.") from exc
        options.validate()
        return options

    def _save_operating_settings(self, analysis: AnalysisOptions) -> None:
        self.settings.simulator_version = int(self.version_var.get())
        self.settings.dc_monitor_percent = analysis.dc_monitor_percent
        self.settings.ac_monitor_percent = analysis.ac_monitor_percent
        self.settings.contingencies_per_file = analysis.contingencies_per_file
        self.settings.use_model_build = self.method_var.get() == "model"
        self.settings.use_distributed = analysis.use_distributed
        self.store.save(self.settings)

    def _prepare_contingency_case(self) -> None:
        if self._busy():
            return
        case = self._selected_case()
        if case is None:
            return
        try:
            contingencies = self._contingency_options()
        except ValueError as exc:
            messagebox.showerror("Contingency setup", str(exc), parent=self.parent)
            return
        suffixes = ["CA_NOT_RUN"]
        if contingencies.n1:
            suffixes.append("N-1")
        if contingencies.n11:
            suffixes.append("N-1-1")
        if contingencies.long_term_p2:
            suffixes.append("LongTerm")
        suggested = default_prepared_case_path(case, "_" + "_".join(suffixes))
        output = filedialog.asksaveasfilename(
            parent=self.parent,
            title="Save the prepared contingency case",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=".pwb",
            filetypes=(("PowerWorld binary", "*.pwb"),),
        )
        if not output:
            return
        self.settings.use_distributed = self.dist_var.get()
        self.store.save(self.settings)
        self._set_status("Building the selected contingencies...")
        try:
            prepared = ContingencyWorkflow(
                PowerWorldClient(), self.settings, log=self._write_log
            ).prepare_combined_case(case, output, contingencies)
            launch_powerworld(prepared, int(self.version_var.get()))
        except (OSError, PowerWorldError, ValueError) as exc:
            self._operation_failed("Prepare contingency case", exc)
            return
        self._set_status("Prepared contingency case saved and opened")
        self._write_log(f"Prepared contingency case complete: {prepared}")

    def _create_pti_file(self) -> None:
        if self._busy():
            return
        case = self._selected_case()
        if case is None:
            return
        try:
            contingencies = self._contingency_options()
        except ValueError as exc:
            messagebox.showerror("Contingency setup", str(exc), parent=self.parent)
            return
        suggested = default_prepared_case_path(case, "_CA").with_suffix(".con")
        output = filedialog.asksaveasfilename(
            parent=self.parent,
            title="Save PTI contingency file",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=".con",
            filetypes=(("PTI contingency file", "*.con"),),
        )
        if not output:
            return
        truncate = messagebox.askyesno(
            "PTI contingency labels",
            "Truncate contingency labels after 12 characters?",
            parent=self.parent,
        )
        self.settings.use_distributed = self.dist_var.get()
        self.store.save(self.settings)
        self._set_status("Creating PTI contingency file...")
        try:
            result = ContingencyWorkflow(
                PowerWorldClient(), self.settings, log=self._write_log
            ).write_pti_contingencies(
                case, output, contingencies, truncate_labels=truncate
            )
        except (OSError, PowerWorldError, ValueError) as exc:
            self._operation_failed("Create PTI contingency file", exc)
            return
        self._set_status("PTI contingency file created")
        self._write_log(f"PTI contingency file complete: {result}")

    def _run_analysis(self) -> None:
        if self._busy():
            return
        cases = self._case_paths()
        if not cases:
            messagebox.showinfo("Run analysis", "Add at least one RAW or PWB case first.", parent=self.parent)
            return
        try:
            contingencies = self._contingency_options()
            analysis = self._analysis_options()
        except ValueError as exc:
            messagebox.showerror("Run analysis", str(exc), parent=self.parent)
            return
        initial_dir = str(default_prepared_case_path(cases[0], "").parent)
        output_dir = filedialog.askdirectory(
            parent=self.parent,
            title="Select output folder for the contingency analysis",
            initialdir=initial_dir,
        )
        if not output_dir:
            return
        self._save_operating_settings(analysis)
        run_settings = deepcopy(self.settings)
        # Snapshot preferences once for the queued cases; a run should not change
        # its AUX mapping or pool because the visible settings object was edited.
        self._set_status(f"Starting analysis for {len(cases)} case(s)...")

        def work(log):
            completed = 0
            all_outputs: list[str] = []
            log_paths: list[str] = []
            warnings: list[str] = []

            def confirm_basecase(case_path, violation_count: int) -> bool:
                # Ask on the UI thread, then block only this worker for the answer.
                # Calling messagebox directly from the COM thread would mix owners.
                response: queue.Queue[bool] = queue.Queue(maxsize=1)
                self._task_queue.put(
                    ("basecase_question", str(case_path), violation_count, response)
                )
                return response.get()

            for index, case in enumerate(cases, start=1):
                # Cases run serially, each with its own workflow/client and logfile.
                # Parallelism belongs inside PowerWorld's selected worker pool.
                if self._stop_event.is_set():
                    raise StopRequested("Stopped before starting the next case.")
                with CaseSessionLog(output_dir, case) as session_log:
                    log_paths.append(str(session_log.path))
                    self._task_queue.put(
                        ("case_log_started", case, str(session_log.path), index, len(cases))
                    )

                    def case_log(message: object) -> None:
                        # Write/flush disk first, then send that exact timestamped
                        # text to the GUI. The on-screen log is not the durable copy.
                        self._task_queue.put(("log", session_log.write(message)))

                    case_log(f"Starting analysis for case {index} of {len(cases)}: {case}")
                    case_log(f"Log file: {session_log.path}")
                    try:
                        result = ContingencyWorkflow(
                            PowerWorldClient(),
                            run_settings,
                            log=case_log,
                            confirm_basecase_violations=confirm_basecase,
                            progress=lambda event: self._task_queue.put(("progress", event)),
                            stop_requested=self._stop_event.is_set,
                        ).run_analysis(case, output_dir, contingencies, analysis)
                    except Exception as exc:
                        case_log(f"ERROR: {exc}")
                        raise
                    completed += 1
                    paths = [str(path) for path in result.outputs]
                    all_outputs.extend(paths)
                    warnings.extend(result.warnings)
                    case_log("Outputs: " + ", ".join(paths))
                    case_log("Case analysis complete.")
            return completed, all_outputs, log_paths, warnings

        def finished(result) -> None:
            completed, _outputs, log_paths, warnings = result
            self.parent.title(APP_TITLE)
            self._set_status(f"Analysis complete for {completed} case(s)")
            notify = messagebox.showwarning if warnings else messagebox.showinfo
            notify(
                "Analysis completed with warnings" if warnings else "Analysis complete",
                f"Completed {completed} case(s).\n\nOutputs and {len(log_paths)} logfile(s) "
                f"were written to:\n{output_dir}" + ("\n\n" + "\n".join(warnings[:8]) if warnings else ""),
                parent=self.parent,
            )

        self._start_background_task("Run analysis", work, finished)

    def _resume_analysis(self) -> None:
        # A manifest identifies one group and its PWB checkpoint, not a new input
        # case or a saved queue. Use a separate log to preserve the failed run's log.
        if self._busy():
            return
        manifest = filedialog.askopenfilename(
            parent=self.parent, title="Resume one saved contingency group",
            filetypes=(("Study recovery record", "*.resume.json"),),
        )
        if not manifest:
            return
        settings = deepcopy(self.settings)

        def work(_log):
            with CaseSessionLog(Path(manifest).parent, Path(manifest).stem + "_resumed") as session_log:
                def log(message):
                    self._task_queue.put(("log", session_log.write(message)))
                try:
                    return ContingencyWorkflow(
                        PowerWorldClient(), settings, log=log,
                        progress=lambda event: self._task_queue.put(("progress", event)),
                        stop_requested=self._stop_event.is_set,
                    ).resume_analysis(manifest)
                except Exception as exc:
                    log(f"STOPPED: {exc}")
                    raise

        def finished(result):
            notify = messagebox.showwarning if result.warnings else messagebox.showinfo
            notify("Resumed group complete", "Outputs:\n" + "\n".join(map(str, result.outputs))
                   + ("\n\n" + "\n".join(result.warnings[:8]) if result.warnings else ""), parent=self.parent)
            self._set_status("Resumed group complete")

        self._clear_log()
        self._start_background_task("Resume analysis", work, finished)

    def _busy(self) -> bool:
        if self._task_running:
            messagebox.showinfo("Study running", "Wait for this study or use Stop safely first.", parent=self.parent)
        return self._task_running

    def _request_stop(self) -> None:
        # Do not kill SimAuto while it is writing results. The workflow observes
        # this signal at safe boundaries, including its retry waits.
        self._stop_event.set()
        self.stop_button.state(("disabled",))
        self._write_log("Stop requested. The current PowerWorld call must finish; its completed stage will be saved before stopping.")

    def _close_window(self) -> None:
        # The worker is a daemon thread; destroying the UI during a solve could
        # exit before checkpointing. Keep the window alive until work has stopped.
        if self._task_running:
            self._request_stop()
            messagebox.showinfo("Stopping safely", "Leave this window open while the current PowerWorld call finishes. "
                                "Close it after the study reports that it stopped.", parent=self.parent)
        else:
            self.parent.destroy()

    def _tick_run_clock(self) -> None:
        if self._task_running:
            now = time.monotonic()
            stop = " | stop pending" if self._stop_event.is_set() else ""
            self.run_info_var.set(f"{self._stage_name}{self._run_detail}\n"
                                  f"Run: {int(now - self._task_started)}s | Stage: {int(now - self._stage_started)}s{stop}")
        self.parent.after(1000, self._tick_run_clock)

    def _task_finished(self, text: str) -> None:
        self._task_running = False
        self.run_button.state(("!disabled",))
        self.stop_button.state(("disabled",))
        self.run_progress.stop()
        self.run_info_var.set(text)

    def _start_background_task(self, title, work, on_success) -> None:
        if self._task_running:
            messagebox.showinfo(
                title,
                "Another PowerWorld operation is already running.",
                parent=self.parent,
            )
            return
        self._task_running = True
        self._stop_event.clear()
        self._task_started = self._stage_started = time.monotonic()
        self._stage_name = title
        self._run_detail = ""
        self.run_progress.configure(mode="indeterminate")
        self.run_progress.start(15)
        self.stop_button.state(("!disabled",))
        self.run_button.state(("disabled",))

        def runner() -> None:
            # COM initialization belongs to the thread that creates/uses SimAuto.
            # Release it in finally for success, cooperative stop, and exceptions.
            pythoncom = None
            try:
                try:
                    import pythoncom as _pythoncom

                    pythoncom = _pythoncom
                    pythoncom.CoInitialize()
                except ImportError:
                    pass
                log = lambda message: self._task_queue.put(("log", str(message)))
                result = work(log)
                self._task_queue.put(("success", on_success, result))
            except StopRequested as exc:
                self._task_queue.put(("stopped", str(exc)))
            except Exception as exc:
                self._task_queue.put(("error", title, exc))
            finally:
                if pythoncom is not None:
                    pythoncom.CoUninitialize()

        threading.Thread(target=runner, name="PowerWorld analysis", daemon=True).start()

    def _poll_task_queue(self) -> None:
        # Bound each drain so a flood of log messages cannot monopolize Tk's
        # event loop. Success callbacks and question dialogs also run here.
        try:
            for _ in range(100):
                event = self._task_queue.get_nowait()
                if event[0] == "log":
                    self._write_log(event[1])
                elif event[0] == "progress":
                    detail = event[1]
                    self._stage_name = detail["stage"]
                    self._stage_started = time.monotonic()
                    self._run_detail = ""
                    if "workers" in detail:
                        self._run_detail = f" | {detail['workers']} requested workers on {detail['computers']} available computers"
                    elif "active" in detail:
                        self._run_detail = f" | {detail['active']:,} active contingencies"
                    self._set_status(self._stage_name)
                elif event[0] == "stopped":
                    self._task_finished("Stopped safely")
                    self._set_status("Stopped safely")
                    self._write_log(event[1])
                    messagebox.showinfo("Stopped safely", event[1], parent=self.parent)
                elif event[0] == "case_log_started":
                    _kind, case_path, log_path, case_number, case_count = event
                    self._clear_log()
                    self._set_status(
                        f"Analyzing case {case_number} of {case_count}: {case_path}"
                    )
                    self.parent.title(f"{APP_TITLE} - {case_path}")
                elif event[0] == "basecase_question":
                    _kind, case_path, violation_count, response = event
                    answer = messagebox.askyesno(
                        "Base-case violations found",
                        f"{case_path} has {violation_count} base-case violation(s).\n\n"
                        "It is recommended that these be corrected before running the study. "
                        "Continue anyway?",
                        parent=self.parent,
                    )
                    response.put(answer)
                elif event[0] == "success":
                    self._task_finished("Complete — see log for results and any warnings")
                    event[1](event[2])
                elif event[0] == "error":
                    self._task_finished("Stopped by an error — see log for recovery information")
                    self._operation_failed(event[1], event[2])
        except queue.Empty:
            pass
        self.parent.after(100, self._poll_task_queue)

    def _operation_failed(self, title: str, error: Exception) -> None:
        self.parent.title(APP_TITLE)
        messagebox.showerror(title, str(error), parent=self.parent)
        self._set_status(f"{title} failed")
        self._write_log(f"ERROR: {error}")

    def _add_cases(self) -> None:
        paths = filedialog.askopenfilenames(parent=self.parent, title="Select PowerWorld cases", filetypes=(("PowerWorld cases", "*.pwb *.raw"), ("PowerWorld binary", "*.pwb"), ("PTI RAW", "*.raw"), ("All files", "*.*")))
        existing = set(self._case_paths())
        added = 0
        for path in paths:
            if path not in existing:
                self.case_list.insert("end", path)
                existing.add(path)
                added += 1
        if added:
            self._set_status(f"Added {added} case(s)")

    def _remove_cases(self) -> None:
        selected = self.case_list.curselection()
        # Delete backwards so removing an earlier row does not shift later indexes.
        for index in reversed(selected):
            self.case_list.delete(index)
        if selected:
            self._set_status(f"Removed {len(selected)} case(s)")

    def _open_aux(self) -> None:
        if self._busy():
            return
        AuxSelectionDialog(self.parent, self.settings, self.store, self._case_paths(), on_saved=self._aux_saved)

    def _aux_saved(self, count: int) -> None:
        self._set_status(f"Saved {count} AUX/CON selection(s)")
        self._write_log(f"Saved {count} AUX/CON selection(s).")

    def _open_extreme_events(self) -> None:
        if self._busy():
            return
        ExtremeEventsDialog(self.parent, self.settings, self.store, on_saved=self._extreme_saved)

    def _extreme_saved(self, count: int) -> None:
        self.extreme_var.set(count > 0)
        self._set_status(f"Selected {count} extreme-event group(s)")
        self._write_log(f"Selected {count} extreme-event group(s).")

    def _version_changed(self, _event=None) -> None:
        # This choice locates the desktop executable for Open in PowerWorld.
        # Automation uses the registered pwrworld.SimulatorAuto COM server.
        self.settings.simulator_version = int(self.version_var.get())
        self.store.save(self.settings)
        self.version_label_var.set(f"Version {self.version_var.get()} selected")
        self._set_status(self.version_label_var.get())

    def _show_about(self) -> None:
        messagebox.showinfo(APP_TITLE, "Python conversion of the legacy NERC TPL CA utility.", parent=self.parent)

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)
        self.parent.update_idletasks()

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
