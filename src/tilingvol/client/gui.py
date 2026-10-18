"""The volunteer window: enter a name, press Start."""

import os
import queue
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from tilingvol import __version__
from tilingvol.client.controller import (
    Controller,
    Event,
    State,
    Status,
    format_duration,
)
from tilingvol.client.key import load_submit_key
from tilingvol.client.memory_guard import installed_ram_mb
from tilingvol.client.settings import Settings, SettingsStore, app_dir

PUMP_MS = 200
MAX_LOG_LINES = 2000
AUTOSTART_DELAY_MS = 1500


class App:
    def __init__(self, root: tk.Tk, controller_factory=Controller) -> None:
        self.root = root
        self.directory = app_dir()
        self.store = SettingsStore(self.directory)
        self.events: queue.Queue[Event] = queue.Queue()
        self.state = State.IDLE
        settings = self.store.load()
        self.controller = controller_factory(
            self.directory, load_submit_key(), self.events.put
        )
        self._build(settings)
        self.controller.launch()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(PUMP_MS, self._pump)

    def _build(self, settings: Settings) -> None:
        root = self.root
        root.title(f"Gun Tiling Search {__version__}")
        root.minsize(560, 520)
        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        form = ttk.Frame(main)
        form.pack(fill="x")
        self.name_var = tk.StringVar(value=settings.name)
        self.ram_var = tk.StringVar(value=f"{settings.max_ram_mb / 1024:.1f}")
        self.threads_var = tk.StringVar(value=str(settings.max_threads))
        self.url_var = tk.StringVar(value=settings.server_url)
        self.width_var = tk.StringVar()
        self.height_var = tk.StringVar()
        total_gb = installed_ram_mb() / 1024
        cores = os.cpu_count() or 1

        ttk.Label(form, text="Your name").grid(row=0, column=0, sticky="w", pady=2)
        self.name_entry = ttk.Entry(form, textvariable=self.name_var, width=30)
        self.name_entry.grid(row=0, column=1, columnspan=2, sticky="we", pady=2)
        ttk.Label(form, text="Max RAM (GB)").grid(row=1, column=0, sticky="w", pady=2)
        self.ram_entry = ttk.Spinbox(
            form,
            textvariable=self.ram_var,
            from_=0.5,
            to=round(total_gb, 1),
            increment=0.5,
            width=8,
        )
        self.ram_entry.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(form, text=f"of {total_gb:.1f} GB installed").grid(
            row=1, column=2, sticky="w"
        )
        ttk.Label(form, text="CPU threads").grid(row=2, column=0, sticky="w", pady=2)
        self.threads_entry = ttk.Spinbox(
            form, textvariable=self.threads_var, from_=1, to=cores, increment=1, width=8
        )
        self.threads_entry.grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(form, text=f"of {cores}").grid(row=2, column=2, sticky="w")
        form.columnconfigure(2, weight=1)

        self.advanced_shown = tk.BooleanVar(value=False)
        self.advanced_check = ttk.Checkbutton(
            main,
            text="Advanced",
            variable=self.advanced_shown,
            command=self._toggle_advanced,
        )
        self.advanced_check.pack(anchor="w", pady=(6, 0))
        self.advanced = ttk.Frame(main)
        ttk.Label(self.advanced, text="Server").grid(
            row=0, column=0, sticky="w", pady=2
        )
        self.url_entry = ttk.Entry(self.advanced, textvariable=self.url_var, width=40)
        self.url_entry.grid(row=0, column=1, columnspan=3, sticky="we", pady=2)
        ttk.Label(self.advanced, text="Request a size (optional)").grid(
            row=1, column=0, sticky="w", pady=2
        )
        self.width_entry = ttk.Entry(
            self.advanced, textvariable=self.width_var, width=6
        )
        self.width_entry.grid(row=1, column=1, sticky="w")
        ttk.Label(self.advanced, text="x").grid(row=1, column=2)
        self.height_entry = ttk.Entry(
            self.advanced, textvariable=self.height_var, width=6
        )
        self.height_entry.grid(row=1, column=3, sticky="w")

        self.buttons = ttk.Frame(main)
        self.buttons.pack(fill="x", pady=8)
        self.start_button = ttk.Button(
            self.buttons, text="Start", command=self.start_clicked
        )
        self.pause_button = ttk.Button(
            self.buttons, text="Pause", command=self.pause_clicked
        )
        self.stop_button = ttk.Button(
            self.buttons, text="Stop", command=self.controller.stop
        )
        self.give_up_button = ttk.Button(
            self.buttons, text="Give Up", command=self.controller.give_up
        )
        for button in (
            self.start_button,
            self.pause_button,
            self.stop_button,
            self.give_up_button,
        ):
            button.pack(side="left", padx=(0, 6))
        self.error_var = tk.StringVar()
        ttk.Label(
            main, textvariable=self.error_var, foreground="#c92a2a", wraplength=520
        ).pack(anchor="w")

        status = ttk.LabelFrame(main, text="Status", padding=8)
        status.pack(fill="x")
        self.status_vars = {}
        for row, label in enumerate(
            ("State", "Size", "Stage", "Stage time", "RAM", "Finished")
        ):
            ttk.Label(status, text=label).grid(row=row, column=0, sticky="w")
            var = tk.StringVar(value="-")
            ttk.Label(status, textvariable=var).grid(
                row=row, column=1, sticky="w", padx=8
            )
            self.status_vars[label] = var

        self.log = ScrolledText(main, height=10, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self._apply_state(State.IDLE)

    def _toggle_advanced(self) -> None:
        if self.advanced_shown.get():
            self.advanced.pack(after=self.advanced_check, fill="x")
        else:
            self.advanced.pack_forget()

    # buttons
    def start_clicked(self) -> None:
        self.error_var.set("")
        try:
            settings = Settings(
                name=self.name_var.get(),
                max_ram_mb=int(float(self.ram_var.get()) * 1024),
                max_threads=int(self.threads_var.get()),
                server_url=self.url_var.get(),
            ).validated()
            requested = self._requested_size()
        except ValueError as error:
            self.error_var.set(str(error))
            return
        self.store.save(settings)
        self.controller.start(settings, requested)

    def pause_clicked(self) -> None:
        if self.state == State.PAUSED:
            self.controller.resume()
        else:
            self.controller.pause()

    def _requested_size(self) -> tuple[int, int] | None:
        w, h = self.width_var.get().strip(), self.height_var.get().strip()
        if not w and not h:
            return None
        if not (w.isdigit() and h.isdigit()):
            raise ValueError("a requested size needs a whole-number width and height")
        return int(w), int(h)

    def close(self) -> None:
        self.status_vars["State"].set("closing, handing the current size back...")
        self.root.update_idletasks()
        self.controller.shutdown()
        self.root.destroy()

    # events
    def _pump(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "status":
                self._show_status(event.status)
            else:
                self._append_log(event.message)
        self.root.after(PUMP_MS, self._pump)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_status(self, status: Status) -> None:
        self._apply_state(status.state)
        v = self.status_vars
        v["State"].set(status.state.value)
        job = status.job
        v["Size"].set(
            f"{job.width} x {job.height} ({job.lane} lane, {job.threads} threads)"
            if job
            else "-"
        )
        if job and status.stage_index >= 0:
            stage = job.stages[status.stage_index]
            v["Stage"].set(
                f"{status.stage_index + 1} of {len(job.stages)}: {stage.describe()}"
            )
            v["Stage time"].set(
                f"{format_duration(status.stage_active_s)} of {format_duration(stage.time_limit_s)}"
            )
        else:
            v["Stage"].set("-")
            v["Stage time"].set("-")
        v["RAM"].set(
            f"{status.ram_used_mb / 1024:.1f} GB used, limit {status.ram_cap_mb / 1024:.1f} GB"
            if status.ram_cap_mb
            else "-"
        )
        v["Finished"].set(str(status.finished))

    def _apply_state(self, state: State) -> None:
        self.state = state
        idle = state == State.IDLE
        busy = state in (State.RUNNING, State.PAUSED)
        self.start_button.configure(
            state="normal" if state in (State.IDLE, State.STOPPED) else "disabled",
            text="Start again" if state == State.STOPPED else "Start",
        )
        self.pause_button.configure(
            state="normal" if busy else "disabled",
            text="Resume" if state == State.PAUSED else "Pause",
        )
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.give_up_button.configure(state="disabled" if idle else "normal")
        field_state = "normal" if idle else "disabled"
        for entry in (
            self.name_entry,
            self.ram_entry,
            self.threads_entry,
            self.url_entry,
            self.width_entry,
            self.height_entry,
        ):
            entry.configure(state=field_state)


def main() -> None:
    root = tk.Tk()
    try:
        app = App(root)
    except RuntimeError as error:
        root.withdraw()
        from tkinter import messagebox

        messagebox.showerror("Gun Tiling Search", str(error))
        root.destroy()
        return
    if os.environ.get("TILINGVOL_AUTOSTART") == "1" and app.name_var.get().strip():
        root.after(
            AUTOSTART_DELAY_MS, app.start_clicked
        )  # e.g. for starting with the computer
    root.mainloop()
