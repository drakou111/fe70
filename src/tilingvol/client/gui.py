"""The fe70 window: enter a name, press Start. With "Parallel sizes" above 1 it runs that many searches at
once, each with an equal share of the CPU threads and RAM; the buttons act on all of them."""

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from tilingvol import __version__
from tilingvol.client.api import ServerError
from tilingvol.client.controller import (
    Controller,
    Event,
    State,
    Status,
    format_duration,
)
from tilingvol.client.key import load_submit_key
from tilingvol.client.memory_guard import installed_ram_mb
from tilingvol.client.release_key import RELEASE_PUBLIC_KEY_HEX
from tilingvol.client.settings import Settings, SettingsStore, app_dir
from tilingvol.client.update import (
    UpdateError,
    Updater,
    installed_executable,
    remove_previous_version,
)
from tilingvol.shared.picture import render_png
from tilingvol.shared.pieces import piece_by_name

APP_TITLE = "fe70"
PUMP_MS = 200
log = logging.getLogger("tilingvol.client")
MAX_LOG_LINES = 2000
AUTOSTART_DELAY_MS = 1500
UPDATE_CHECK_S = 15 * 60
FIRST_UPDATE_CHECK_S = 5.0
APP_EVENT = -1  # events that belong to the window, not to one search
SHUTDOWN_TIMEOUT_S = 20.0
PICTURE_MAX_PX = 240
BUTTON_HELP_DEFAULT = "Point at a button to see what it does."
BUTTON_HELP = {
    "start": "Start: get a size from the server and search it, then the next one, until you stop.",
    "start_again": "Start again: search your reserved size again from the beginning.",
    "pause": "Pause: freeze the search instantly (CPU drops to zero, its RAM stays in use). "
    "Resume continues exactly where it was.",
    "resume": "Resume: continue the frozen search exactly where it was.",
    "stop": "Stop & free RAM: end the search and free its memory. The size stays reserved for you; "
    "Start again redoes it from the beginning.",
    "give_up": "Give back size: end the search and return the size to the server, so someone else "
    "can take it.",
}


def combined_state(states: list[State]) -> State:
    """One state for the buttons: running if any search runs, then paused, then stopped."""
    for state in (State.RUNNING, State.PAUSED, State.STOPPED):
        if state in states:
            return state
    return State.IDLE


class App:
    def __init__(
        self,
        root: tk.Tk,
        controller_factory=Controller,
        update_key_hex: str = RELEASE_PUBLIC_KEY_HEX,
        install_path: Path | None = None,
    ) -> None:
        self.root = root
        self.directory = app_dir()
        self.store = SettingsStore(self.directory)
        self.events: queue.Queue[tuple[int, Event]] = queue.Queue()
        self.state = State.IDLE
        self._controller_factory = controller_factory
        self._key = load_submit_key()
        self.slots: list[Controller] = []
        self.slot_status: dict[int, Status] = {}
        settings = self.store.load()
        self._server_url = settings.server_url
        self._closing = threading.Event()
        self._ready_update: tuple[Updater, Path, str] | None = None
        self._update_key = bytes.fromhex(update_key_hex) if update_key_hex else b""
        self._install_path = install_path or installed_executable()
        self._build(settings)
        if self._update_key:
            remove_previous_version(self._install_path)
            threading.Thread(
                target=self._update_loop, name="updates", daemon=True
            ).start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(PUMP_MS, self._pump)

    def _build(self, settings: Settings) -> None:
        root = self.root
        root.title(f"{APP_TITLE} {__version__}")
        root.minsize(600, 560)
        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        self.update_bar = ttk.Frame(main)
        self.update_var = tk.StringVar()
        ttk.Label(self.update_bar, textvariable=self.update_var, wraplength=420).pack(
            side="left"
        )
        self.update_button = ttk.Button(
            self.update_bar, text="Restart now", command=self.restart_clicked
        )

        form = self.form = ttk.Frame(main)
        form.pack(fill="x")
        self.name_var = tk.StringVar(value=settings.name)
        self.ram_var = tk.StringVar(value=f"{settings.max_ram_mb / 1024:.1f}")
        self.threads_var = tk.StringVar(value=str(settings.max_threads))
        self.parallel_var = tk.StringVar(value=str(settings.parallel))
        self.url_var = tk.StringVar(value=settings.server_url)
        self.token_var = tk.StringVar(value=settings.access_token)
        self.no_limit_var = tk.BooleanVar(value=settings.no_time_limit)
        self.tree_var = tk.BooleanVar(value=settings.tree_progress)
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
        ttk.Label(form, text="Parallel sizes").grid(row=3, column=0, sticky="w", pady=2)
        self.parallel_entry = ttk.Spinbox(
            form,
            textvariable=self.parallel_var,
            from_=1,
            to=cores,
            increment=1,
            width=8,
        )
        self.parallel_entry.grid(row=3, column=1, sticky="w", pady=2)
        self.share_var = tk.StringVar()
        ttk.Label(form, textvariable=self.share_var).grid(row=3, column=2, sticky="w")
        for var in (self.ram_var, self.threads_var, self.parallel_var):
            var.trace_add("write", lambda *_: self._show_share())
        self._show_share()
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
        ttk.Label(self.advanced, text="Access token").grid(
            row=2, column=0, sticky="w", pady=2
        )
        self.token_entry = ttk.Entry(
            self.advanced, textvariable=self.token_var, width=40, show="*"
        )
        self.token_entry.grid(row=2, column=1, columnspan=3, sticky="we", pady=2)
        self.no_limit_check = ttk.Checkbutton(
            self.advanced,
            text="No time limit: search each size until it is decided (can take days)",
            variable=self.no_limit_var,
        )
        self.no_limit_check.grid(row=3, column=0, columnspan=4, sticky="w", pady=2)
        self.tree_check = ttk.Checkbutton(
            self.advanced,
            text="Show tree progress (closed X/Y): half the threads use CP-SAT's tree search instead",
            variable=self.tree_var,
        )
        self.tree_check.grid(row=4, column=0, columnspan=4, sticky="w", pady=2)
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
            self.buttons, text="Stop & free RAM", command=lambda: self._each("stop")
        )
        self.give_up_button = ttk.Button(
            self.buttons, text="Give back size", command=lambda: self._each("give_up")
        )
        self.help_var = tk.StringVar(value=BUTTON_HELP_DEFAULT)
        for button, key in (
            (self.start_button, "start"),
            (self.pause_button, "pause"),
            (self.stop_button, "stop"),
            (self.give_up_button, "give_up"),
        ):
            button.pack(side="left", padx=(0, 6))
            button.bind(
                "<Enter>", lambda _e, k=key: self.help_var.set(self._button_help(k))
            )
            button.bind("<Leave>", lambda _e: self.help_var.set(BUTTON_HELP_DEFAULT))
        ttk.Label(
            main, textvariable=self.help_var, foreground="#66706b", wraplength=560
        ).pack(anchor="w")
        self.error_var = tk.StringVar()
        ttk.Label(
            main, textvariable=self.error_var, foreground="#c92a2a", wraplength=540
        ).pack(anchor="w")

        status = ttk.LabelFrame(main, text="Status", padding=8)
        status.pack(fill="x")
        self.summary_var = tk.StringVar(value="Idle. Finished sizes: 0")
        ttk.Label(status, textvariable=self.summary_var).pack(anchor="w")
        columns = ("size", "stage", "time", "progress", "ram")
        self.table = ttk.Treeview(status, columns=columns, show="headings", height=3)
        for column, title, width in (
            ("size", "Size", 190),
            ("stage", "Stage", 150),
            ("time", "Stage time", 130),
            ("progress", "Progress", 90),
            ("ram", "RAM", 100),
        ):
            self.table.heading(column, text=title, anchor="w")
            self.table.column(column, width=width, anchor="w", stretch=True)
        self.table.pack(fill="x", pady=(6, 0))

        self.picture_frame = ttk.LabelFrame(main, text="Latest picture", padding=8)
        self.picture_label = ttk.Label(self.picture_frame)
        self.picture_label.pack(side="left")
        side = ttk.Frame(self.picture_frame)
        side.pack(side="left", fill="y", padx=(12, 0))
        self.picture_var = tk.StringVar()
        ttk.Label(side, textvariable=self.picture_var, wraplength=260).pack(anchor="w")
        ttk.Button(side, text="Open pictures folder", command=self.open_pictures).pack(
            anchor="w", pady=(8, 0)
        )
        self._picture_image: tk.PhotoImage | None = None

        self.log = ScrolledText(main, height=10, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self._apply_state(State.IDLE)

    def _button_help(self, key: str) -> str:
        if key == "pause" and self.state == State.PAUSED:
            return BUTTON_HELP["resume"]
        if key == "start" and self.state == State.STOPPED:
            return BUTTON_HELP["start_again"]
        return BUTTON_HELP[key]

    def _toggle_advanced(self) -> None:
        if self.advanced_shown.get():
            self.advanced.pack(after=self.advanced_check, fill="x")
        else:
            self.advanced.pack_forget()

    def _show_share(self) -> None:
        try:
            parallel = max(1, int(self.parallel_var.get()))
            threads = int(self.threads_var.get()) // parallel
            ram_gb = float(self.ram_var.get()) / parallel
        except ValueError:
            self.share_var.set("")
            return
        self.share_var.set(f"each: {threads} threads, {ram_gb:.1f} GB")

    # buttons
    def start_clicked(self) -> None:
        self.error_var.set("")
        try:
            settings = Settings(
                name=self.name_var.get(),
                max_ram_mb=int(float(self.ram_var.get()) * 1024),
                max_threads=int(self.threads_var.get()),
                server_url=self.url_var.get(),
                parallel=int(self.parallel_var.get()),
                access_token=self.token_var.get(),
                no_time_limit=self.no_limit_var.get(),
                tree_progress=self.tree_var.get(),
            ).validated()
            requested = self._requested_size()
        except ValueError as error:
            self.error_var.set(str(error))
            return
        self.store.save(settings)
        self._server_url = settings.server_url
        if self.state == State.IDLE:
            self._set_slot_count(settings.parallel)
        for index, controller in enumerate(self.slots):
            controller.start(
                settings.slot_settings(), requested if index == 0 else None
            )

    def pause_clicked(self) -> None:
        self._each("resume" if self.state == State.PAUSED else "pause")

    def _each(self, command: str) -> None:
        for controller in self.slots:
            getattr(controller, command)()

    def _set_slot_count(self, count: int) -> None:
        """Only while idle: rebuild the searches when the Parallel sizes setting changed (their log labels
        depend on the count)."""
        if len(self.slots) == count:
            return
        for controller in self.slots:
            controller.shutdown()
        self.slots.clear()
        self.slot_status.clear()
        while len(self.slots) < count:
            index = len(self.slots)
            label = str(index + 1) if count > 1 else ""
            controller = self._controller_factory(
                self.directory,
                self._key,
                lambda event, i=index: self.events.put((i, event)),
                label=label,
            )
            controller.launch()
            self.slots.append(controller)

    def _requested_size(self) -> tuple[int, int] | None:
        w, h = self.width_var.get().strip(), self.height_var.get().strip()
        if not w and not h:
            return None
        if not (w.isdigit() and h.isdigit()):
            raise ValueError("a requested size needs a whole-number width and height")
        return int(w), int(h)

    def close(self) -> None:
        self._closing.set()
        self.summary_var.set("Closing, handing the current sizes back...")
        self.root.update_idletasks()
        stoppers = [
            threading.Thread(target=c.shutdown, args=(SHUTDOWN_TIMEOUT_S,))
            for c in self.slots
        ]
        for thread in stoppers:
            thread.start()
        for thread in stoppers:
            thread.join(SHUTDOWN_TIMEOUT_S + 5)
        self.root.destroy()

    def restart_clicked(self) -> None:
        """Swap in the downloaded version and start it; current sizes go back to the server."""
        if self._ready_update is None:
            return
        updater, downloaded, version = self._ready_update
        try:
            new_app = updater.install(downloaded)
        except (UpdateError, OSError) as error:
            self.update_var.set(f"Could not install version {version}: {error}")
            self.update_button.pack_forget()
            return
        self.close()
        # a fresh, independent instance (not a child sharing this one's unpacked files)
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        subprocess.Popen([str(new_app)], env=env, close_fds=True)

    # updates (background thread; talks to the window only through the event queue)
    def _update_loop(self) -> None:
        if self._closing.wait(FIRST_UPDATE_CHECK_S):
            return
        while True:
            try:
                self._check_for_update()
            except (UpdateError, ServerError, OSError, ValueError) as error:
                self._update_log(f"Update check failed: {error}")
            if self._closing.wait(UPDATE_CHECK_S):
                return

    def _update_log(self, message: str) -> None:
        log.info(message)
        self.events.put((APP_EVENT, Event("log", message)))

    def _check_for_update(self) -> None:
        updater = Updater(self._server_url, self._update_key, self._install_path)
        release = updater.check()
        if release is None or (
            self._ready_update and self._ready_update[2] == release.version
        ):
            return
        if not updater.can_self_replace:
            message = f"Version {release.version} is available: {updater.download_url(release)}"
            self.events.put((APP_EVENT, Event("update", message)))
            return
        self._update_log(f"Downloading version {release.version} in the background...")
        downloaded = updater.download(release)
        self._ready_update = (updater, downloaded, release.version)
        self._update_log(f"Version {release.version} downloaded and verified.")
        message = f"Version {release.version} is ready. Restarting hands the current sizes back."
        self.events.put((APP_EVENT, Event("update", message)))

    # events
    def _pump(self) -> None:
        status_changed = False
        while True:
            try:
                slot, event = self.events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "status":
                if slot < len(self.slots):
                    self.slot_status[slot] = event.status
                    status_changed = True
            elif event.kind == "update":
                self._show_update(event.message)
            elif event.kind == "picture" and event.picture:
                threading.Thread(
                    target=self._save_picture, args=(event.picture,), daemon=True
                ).start()
            elif event.kind == "picture_ready":
                self._show_picture(Path(event.message))
            else:
                self._append_log(event.message)
        if status_changed:
            self._show_status()
        self.root.after(PUMP_MS, self._pump)

    def pictures_dir(self) -> Path:
        folder = self.directory / "pictures"
        folder.mkdir(exist_ok=True)
        return folder

    def _save_picture(self, picture: tuple[str, int, int, int, list]) -> None:
        """Background thread: render and save the PNG, then hand the file to the window."""
        piece, width, height, border, placements = picture
        name = (
            f"{width}x{height}-tiling.png"
            if border == 0
            else f"{width}x{height}-border{border}.png"
        )
        path = self.pictures_dir() / name
        try:
            path.write_bytes(
                render_png(piece_by_name(piece), width, height, placements)
            )
        except (ValueError, IndexError, OSError) as error:
            log.exception("could not save the picture %s", name)
            self.events.put(
                (APP_EVENT, Event("log", f"Could not save the picture {name}: {error}"))
            )
            return
        self.events.put((APP_EVENT, Event("picture_ready", str(path))))

    def _show_picture(self, path: Path) -> None:
        image = tk.PhotoImage(file=str(path))
        factor = max(1, -(-max(image.width(), image.height()) // PICTURE_MAX_PX))
        self._picture_image = image.subsample(factor) if factor > 1 else image
        self.picture_label.configure(image=self._picture_image)
        size, _, kind = path.stem.partition("-")
        what = (
            "A COMPLETE TILING!"
            if kind == "tiling"
            else f"{kind.replace('border', 'border depth ')} filled"
        )
        self.picture_var.set(
            f"{size.replace('x', ' x ')}: {what}\nSaved as {path.name}"
        )
        if not self.picture_frame.winfo_ismapped():
            self.picture_frame.pack(fill="x", pady=(8, 0), before=self.log)

    def open_pictures(self) -> None:
        folder = str(self.pictures_dir())
        if sys.platform == "win32":
            os.startfile(folder)
        else:
            subprocess.Popen(
                ["open" if sys.platform == "darwin" else "xdg-open", folder]
            )

    def _show_update(self, message: str) -> None:
        self.update_var.set(message)
        self.update_bar.pack(fill="x", pady=(0, 8), before=self.form)
        if self._ready_update is not None:
            self.update_button.pack(side="right")

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def slot_rows(self) -> list[tuple[str, str, str, str, str]]:
        rows = []
        for index in range(len(self.slots)):
            status = self.slot_status.get(index)
            job = status.job if status else None
            if job is None:
                waiting = status.state.value if status else "starting"
                rows.append(("-", waiting, "-", "-", "-"))
                continue
            size = f"{job.width} x {job.height} ({job.threads} threads)"
            if status.stage_index >= 0:
                stage = job.stages[status.stage_index]
                stage_text = (
                    f"{status.stage_index + 1}/{len(job.stages)} {stage.describe()}"
                )
                time_text = (
                    f"{format_duration(status.stage_active_s)} / "
                    f"{format_duration(stage.time_limit_s)}"
                )
            else:
                stage_text, time_text = status.state.value, "-"
            ram = (
                f"{status.ram_used_mb / 1024:.1f} / {status.ram_cap_mb / 1024:.1f} GB"
                if status.ram_cap_mb
                else "-"
            )
            progress = (
                f"{status.progress[0]}/{status.progress[1]}" if status.progress else "-"
            )
            rows.append((size, stage_text, time_text, progress, ram))
        return rows

    def _show_status(self) -> None:
        states = [s.state for s in self.slot_status.values()]
        self._apply_state(combined_state(states))
        finished = sum(s.finished for s in self.slot_status.values())
        running = sum(1 for s in states if s == State.RUNNING)
        self.summary_var.set(
            f"{self.state.value.capitalize()}: {running} of {len(self.slots)} searches "
            f"running. Finished sizes: {finished}"
        )
        self.table.delete(*self.table.get_children())
        for row in self.slot_rows():
            self.table.insert("", "end", values=row)

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
            self.parallel_entry,
            self.url_entry,
            self.token_entry,
            self.no_limit_check,
            self.tree_check,
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

        messagebox.showerror(APP_TITLE, str(error))
        root.destroy()
        return
    if os.environ.get("TILINGVOL_AUTOSTART") == "1" and app.name_var.get().strip():
        # e.g. for starting with the computer
        root.after(AUTOSTART_DELAY_MS, app.start_clicked)
    root.mainloop()
