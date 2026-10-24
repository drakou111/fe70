"""The fe70 window: enter a name, press Start. Every other field may stay empty, which means the default:
the server picks what to search, and this computer gives half its RAM and all but one CPU thread. With
"Parallel searches" above 1 it runs that many searches at once, each with an equal share of the threads and
RAM; the buttons act on all of them."""

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
from tilingvol.client.settings import (
    Form,
    SettingsStore,
    app_dir,
    default_ram_mb,
    default_server_url,
    default_threads,
)
from tilingvol.client.update import (
    UpdateError,
    Updater,
    installed_executable,
    remove_previous_version,
)
from tilingvol.client.width_queue import WidthQueue
from tilingvol.shared.picture import render_png
from tilingvol.shared.pieces import piece_by_name
from tilingvol.shared.protocol import SEARCH_BOARD, SEARCH_SYMMETRIC, SEARCH_WIDTH

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
HUNT_PICTURE_PREFIX = "sym"
SEARCH_LABELS = {
    SEARCH_WIDTH: "Strip widths",
    SEARCH_BOARD: "Rectangle sizes (board)",
    SEARCH_SYMMETRIC: "Symmetric hunt",
}
SEARCH_HINTS = {
    SEARCH_WIDTH: "Fills one width's half-strip deeper and deeper; a depth that cannot be filled rules "
    "out every rectangle with that side.",
    SEARCH_BOARD: "Searches one rectangle size completely: impossible, or a tiling.",
    SEARCH_SYMMETRIC: "Only tilings that look the same after a half turn: much faster; its 'impossible' "
    "rules out symmetric tilings only.",
}
# per search: (request placeholder, request hint, depths placeholder, depths hint)
SEARCH_FIELDS = {
    SEARCH_WIDTH: (
        "server picks",
        "or widths, like 149, 150, 151",
        "server's",
        "or strip depths, like 48, 56, 64",
    ),
    SEARCH_BOARD: (
        "server picks",
        "or a size, like 150 x 168",
        "server's",
        "or border depths, like 24, 28; full search last",
    ),
    SEARCH_SYMMETRIC: (
        "server picks",
        "or a size, like 150 x 168",
        "server's",
        "or border depths, like 24, 28; full search last",
    ),
}
MUTED = "#66706b"
FIELD_WIDTH = 26
LABEL_WIDTH_PX = 130
SECTION_INDENT_PX = 12  # a section's border and padding
CONTACT = "Drakou111 on Discord"
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


class HintEntry(ttk.Entry):
    """An entry that shows grey default text while it is empty and not being typed in."""

    def __init__(self, master, placeholder: str, width: int, show: str = "") -> None:
        super().__init__(master, width=width)
        self._placeholder = placeholder
        self._show = show
        self._hinted = False
        self._listeners: list = []
        self.bind("<FocusIn>", lambda _e: self._unhint())
        self.bind("<FocusOut>", lambda _e: self._refresh())
        self.bind("<KeyRelease>", lambda _e: self._changed())
        self._refresh()

    @property
    def value(self) -> str:
        return "" if self._hinted else self.get()

    def set_value(self, text: str) -> None:
        with self._editable():
            self.delete(0, "end")
            self._hinted = False
            self.configure(foreground="", show=self._show)
            self.insert(0, text)
        self._refresh()
        self._changed()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        if self._hinted:
            with self._editable():
                self.delete(0, "end")
                self.insert(0, text)

    def on_change(self, callback) -> None:
        self._listeners.append(callback)

    def _changed(self) -> None:
        for callback in self._listeners:
            callback()

    def _unhint(self) -> None:
        if self._hinted:
            with self._editable():
                self.delete(0, "end")
                self.configure(foreground="", show=self._show)
            self._hinted = False

    def _refresh(self) -> None:
        if self.get() or self.focus_get() is self:
            return
        with self._editable():
            self.configure(foreground=MUTED, show="")
            self.insert(0, self._placeholder)
        self._hinted = True

    def _editable(self):
        """Changing the text of a disabled entry needs it enabled for a moment."""
        entry = self

        class Enabled:
            def __enter__(self) -> None:
                self.state = str(entry.cget("state"))
                entry.configure(state="normal")

            def __exit__(self, *exc) -> None:
                entry.configure(state=self.state)

        return Enabled()


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
        form = self.store.load()
        self._server_url = form.server_url.strip() or default_server_url()
        self._closing = threading.Event()
        self._ready_update: tuple[Updater, Path, str] | None = None
        self._update_key = bytes.fromhex(update_key_hex) if update_key_hex else b""
        self._install_path = install_path or installed_executable()
        self._build(form)
        if self._update_key:
            remove_previous_version(self._install_path)
            threading.Thread(
                target=self._update_loop, name="updates", daemon=True
            ).start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(PUMP_MS, self._pump)

    def _build(self, form: Form) -> None:
        root = self.root
        root.title(f"{APP_TITLE} {__version__}")
        root.minsize(640, 600)
        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        self.update_bar = ttk.Frame(main)
        self.update_var = tk.StringVar()
        ttk.Label(self.update_bar, textvariable=self.update_var, wraplength=480).pack(
            side="left"
        )
        self.update_button = ttk.Button(
            self.update_bar, text="Restart now", command=self.restart_clicked
        )

        self.form = ttk.Frame(main)
        self.form.pack(fill="x")
        top = ttk.Frame(self.form)
        top.pack(fill="x", pady=(0, 8))
        top.columnconfigure(0, minsize=LABEL_WIDTH_PX + SECTION_INDENT_PX)
        top.columnconfigure(2, weight=1)
        ttk.Label(top, text="Your name").grid(row=0, column=0, sticky="w")
        self.name_entry = HintEntry(
            top, "shown next to your results", width=FIELD_WIDTH
        )
        self.name_entry.grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(top, text="Empty fields use the default.", foreground=MUTED).grid(
            row=0, column=2, sticky="e"
        )

        search = ttk.LabelFrame(self.form, text="What to search", padding=(10, 6))
        search.pack(fill="x")
        search.columnconfigure(0, minsize=LABEL_WIDTH_PX)
        search.columnconfigure(2, weight=1)
        self.search_var = tk.StringVar(value=SEARCH_LABELS[form.search])
        ttk.Label(search, text="Search").grid(row=0, column=0, sticky="w", pady=3)
        self.search_box = ttk.Combobox(
            search,
            textvariable=self.search_var,
            values=list(SEARCH_LABELS.values()),
            state="readonly",
            width=FIELD_WIDTH - 3,
        )
        self.search_box.grid(row=0, column=1, sticky="w", padx=8, pady=3)
        self.search_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_search())
        self.search_hint_var = tk.StringVar()
        ttk.Label(
            search,
            textvariable=self.search_hint_var,
            foreground=MUTED,
            wraplength=480,
        ).grid(row=1, column=1, columnspan=2, sticky="w", padx=8, pady=(0, 4))
        self.request_entry = self._row(search, 2, "Request", "")
        self.stages_entry = self._row(search, 3, "Depths", "")
        self.time_entry = self._row(
            search, 4, "Time limit", "server's", "hours per stage; 0 = no limit"
        )
        self.request_hint_var = tk.StringVar()
        self.stages_hint_var = tk.StringVar()
        for row, var in ((2, self.request_hint_var), (3, self.stages_hint_var)):
            search.grid_slaves(row=row, column=2)[0].configure(textvariable=var)

        computer = ttk.LabelFrame(self.form, text="This computer", padding=(10, 6))
        computer.pack(fill="x", pady=(8, 0))
        computer.columnconfigure(0, minsize=LABEL_WIDTH_PX)
        computer.columnconfigure(2, weight=1)
        total_gb = installed_ram_mb() / 1024
        self.ram_entry = self._row(
            computer,
            0,
            "RAM (GB)",
            f"{default_ram_mb() / 1024:.1f}",
            f"of {total_gb:.1f} GB installed",
        )
        self.threads_entry = self._row(
            computer,
            1,
            "CPU threads",
            str(default_threads()),
            f"of {os.cpu_count() or 1}",
        )
        self.parallel_entry = self._row(computer, 2, "Parallel searches", "1", "")
        self.share_var = tk.StringVar()
        computer.grid_slaves(row=2, column=2)[0].configure(textvariable=self.share_var)
        self.tree_var = tk.BooleanVar(value=form.tree_progress)
        self.tree_check = ttk.Checkbutton(
            computer,
            text="Show tree progress (closed X/Y); half the threads then use CP-SAT's tree search",
            variable=self.tree_var,
        )
        self.tree_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(3, 0))

        connection = ttk.LabelFrame(self.form, text="Connection", padding=(10, 6))
        connection.pack(fill="x", pady=(8, 0))
        connection.columnconfigure(0, minsize=LABEL_WIDTH_PX)
        connection.columnconfigure(2, weight=1)
        self.url_entry = self._row(connection, 0, "Server", default_server_url(), "")
        self.token_entry = self._row(
            connection,
            1,
            "Access token",
            "",
            f"ask {CONTACT} for one",
            show="*",
        )

        for entry, value in (
            (self.name_entry, form.name),
            (self.request_entry, form.request),
            (self.stages_entry, form.stages),
            (self.time_entry, form.time_limit_hours),
            (self.ram_entry, form.ram_gb),
            (self.threads_entry, form.threads),
            (self.parallel_entry, form.parallel),
            (self.url_entry, form.server_url),
            (self.token_entry, form.access_token),
        ):
            entry.set_value(value)
        for entry in (self.ram_entry, self.threads_entry, self.parallel_entry):
            entry.on_change(self._show_share)
        self._show_share()

        self.buttons = ttk.Frame(main)
        self.buttons.pack(fill="x", pady=(10, 4))
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
            main, textvariable=self.help_var, foreground=MUTED, wraplength=600
        ).pack(anchor="w")
        self.error_var = tk.StringVar()
        ttk.Label(
            main, textvariable=self.error_var, foreground="#c92a2a", wraplength=600
        ).pack(anchor="w")

        status = ttk.LabelFrame(main, text="Status", padding=8)
        status.pack(fill="x", pady=(4, 0))
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

        self.log = ScrolledText(main, height=8, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self._apply_state(State.IDLE)

    @staticmethod
    def _row(
        parent: ttk.Frame,
        row: int,
        label: str,
        placeholder: str,
        hint: str = "",
        show: str = "",
    ) -> "HintEntry":
        """label | entry (grey default text when empty) | grey hint."""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = HintEntry(parent, placeholder, width=FIELD_WIDTH, show=show)
        entry.grid(row=row, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(parent, text=hint, foreground=MUTED).grid(
            row=row, column=2, sticky="w"
        )
        return entry

    def _search(self) -> str:
        label = self.search_var.get()
        return next(k for k, v in SEARCH_LABELS.items() if v == label)

    def _apply_search(self) -> None:
        """Only words change with the search; every field stays where it is."""
        search = self._search()
        self.search_hint_var.set(SEARCH_HINTS[search])
        request, request_hint, stages, stages_hint = SEARCH_FIELDS[search]
        self.request_entry.set_placeholder(request)
        self.request_hint_var.set(request_hint)
        self.stages_entry.set_placeholder(stages)
        self.stages_hint_var.set(stages_hint)

    def form_values(self) -> Form:
        return Form(
            name=self.name_entry.value,
            search=self._search(),
            request=self.request_entry.value,
            stages=self.stages_entry.value,
            time_limit_hours=self.time_entry.value,
            ram_gb=self.ram_entry.value,
            threads=self.threads_entry.value,
            parallel=self.parallel_entry.value,
            tree_progress=self.tree_var.get(),
            server_url=self.url_entry.value,
            access_token=self.token_entry.value,
        )

    def _button_help(self, key: str) -> str:
        if key == "pause" and self.state == State.PAUSED:
            return BUTTON_HELP["resume"]
        if key == "start" and self.state == State.STOPPED:
            return BUTTON_HELP["start_again"]
        return BUTTON_HELP[key]

    def _show_share(self) -> None:
        form = self.form_values()
        try:
            parallel = int(form.parallel) if form.parallel.strip() else 1
            threads = (
                int(form.threads) if form.threads.strip() else default_threads()
            ) // max(1, parallel)
            ram_gb = (
                float(form.ram_gb) if form.ram_gb.strip() else default_ram_mb() / 1024
            ) / max(1, parallel)
        except ValueError:
            self.share_var.set("")
            return
        self.share_var.set(f"each: {threads} threads, {ram_gb:.1f} GB")

    # buttons
    def start_clicked(self) -> None:
        self.error_var.set("")
        form = self.form_values()
        try:
            resolved = form.resolve()
        except ValueError as error:
            self.error_var.set(str(error))
            return
        self.store.save(form)
        settings = resolved.settings
        widths = (
            WidthQueue(resolved.requested_widths) if resolved.requested_widths else None
        )
        self._server_url = settings.server_url
        if self.state == State.IDLE:
            self._set_slot_count(settings.parallel)
        for index, controller in enumerate(self.slots):
            controller.start(
                settings.slot_settings(),
                resolved.requested_size if index == 0 else None,
                widths,
            )

    def pause_clicked(self) -> None:
        self._each("resume" if self.state == State.PAUSED else "pause")

    def _each(self, command: str) -> None:
        for controller in self.slots:
            getattr(controller, command)()

    def _set_slot_count(self, count: int) -> None:
        """Only while idle: rebuild the searches when the Parallel searches setting changed (their log
        labels depend on the count)."""
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
                    target=self._save_picture,
                    args=(event.picture, event.hunt, event.strip),
                    daemon=True,
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

    def _save_picture(
        self,
        picture: tuple[str, int, int, int, list],
        hunt: bool = False,
        strip: bool = False,
    ) -> None:
        """Background thread: render and save the PNG, then hand the file to the window."""
        piece, width, height, border, placements = picture
        kind = "tiling" if border == 0 else f"border{border}"
        prefix = HUNT_PICTURE_PREFIX if hunt else ""
        name = (
            f"{width}-strip{border}.png"
            if strip
            else f"{width}x{height}-{prefix}{kind}.png"
        )
        path = self.pictures_dir() / name
        try:
            path.write_bytes(
                render_png(piece_by_name(piece), width, height, placements, hunt)
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
        hunt = kind.startswith(HUNT_PICTURE_PREFIX)
        kind = kind.removeprefix(HUNT_PICTURE_PREFIX)
        if kind.startswith("strip"):
            what = f"strip depth {kind.removeprefix('strip')} filled"
            size = f"width {size}"
        else:
            what = (
                "A COMPLETE TILING!"
                if kind == "tiling"
                else f"{'symmetric ' if hunt else ''}{kind.replace('border', 'border depth ')} filled"
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
            kind = " symmetric" if job.hunt else ""
            size = (
                f"strip width {job.width} ({job.threads} threads)"
                if job.strip
                else f"{job.width} x {job.height}{kind} ({job.threads} threads)"
            )
            if status.stage_index >= 0:
                stage = job.stages[status.stage_index]
                stage_text = (
                    f"{status.stage_index + 1}/{len(job.stages)} {stage.describe()}"
                )
                time_text = (
                    f"{format_duration(status.stage_active_s)} / "
                    f"{stage.describe_limit()}"
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
            self.request_entry,
            self.stages_entry,
            self.time_entry,
            self.ram_entry,
            self.threads_entry,
            self.parallel_entry,
            self.tree_check,
            self.url_entry,
            self.token_entry,
        ):
            entry.configure(state=field_state)
        self.search_box.configure(state="readonly" if idle else "disabled")
        self._apply_search()


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
    if os.environ.get("TILINGVOL_AUTOSTART") == "1" and app.name_entry.value.strip():
        # e.g. for starting with the computer
        root.after(AUTOSTART_DELAY_MS, app.start_clicked)
    root.mainloop()
