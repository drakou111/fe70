"""Job state machine shared by the GUI and headless runs. One worker thread owns all server traffic and job
state; a watchdog thread enforces the RAM cap and the stage time limits so slow network calls never delay
it. The GUI talks to it only through commands and receives Events."""

import logging
import logging.handlers
import multiprocessing
import os
import queue
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import psutil

from tilingvol import __version__
from tilingvol.client import memory_guard
from tilingvol.client.api import ApiClient, NoJobAvailable, ServerError
from tilingvol.client.exact_runner import (
    MIN_FREE_DISK_MB,
    MIN_FREE_SCRATCH_MB,
    MSG_EXACT_DONE,
    MSG_EXACT_ROW,
    ExactBinaryMissing,
    ExactFiles,
    ExactProcess,
    checkpointed_widths,
)
from tilingvol.client.settings import PendingResults, Settings
from tilingvol.client.solver_process import (
    MSG_ERROR,
    MSG_FILLED,
    MSG_FRAME_FEASIBLE,
    MSG_IMPOSSIBLE,
    MSG_POSSIBLE,
    MSG_PROGRESS,
    MSG_STAGE_START,
    MSG_STOPPED,
    SolverTask,
    run_task,
)
from tilingvol.client.width_queue import WidthQueue
from tilingvol.shared import token
from tilingvol.shared.pieces import piece_by_name
from tilingvol.shared.protocol import (
    RELEASE_CLOSED,
    RELEASE_ERROR,
    RELEASE_GAVE_UP,
    RELEASE_MEMORY,
    SEARCH_EXACT,
    SEARCH_FOREVER,
    SEARCH_WIDTH,
    VERDICT_FILLED,
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
    ExactOutcome,
    Job,
    ResultSubmission,
)

LOOP_S = 0.25
WATCHDOG_S = 0.25
STATUS_S = 1.0
NO_JOB_RETRY_S = 300.0
UNREACHABLE_RETRY_S = 60.0
RETRYABLE_STATUSES = (0, 429, 502, 503, 504)  # unreachable, rate limited, proxy errors
PENDING_RETRY_S = 120.0
MAX_MEMORY_FAILURES = 3
MAX_CRASHES = 3
CRASH_AS_MEMORY_FRACTION = 0.8
KILL_JOIN_S = 10.0
LOG_FILE_BYTES = 1 << 20
MIN_TREE_THREADS = 4
GONE = 410  # the server's answer to a requested width that is already decided

log = logging.getLogger("tilingvol.client")


def unroll(placements: list, stage) -> list:
    """A repeating filling with its block stacked twice more, for the picture."""
    top = stage.periodic_start + stage.period
    block = [p for p in placements if stage.periodic_start <= p[2] < top]
    return list(placements) + [
        [o, x, y + copy * stage.period] for copy in (1, 2) for o, x, y in block
    ]


class State(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Status:
    state: State
    job: Job | None = None
    stage_index: int = -1
    stage_active_s: float = 0.0
    total_active_s: float = 0.0
    ram_used_mb: int = 0
    ram_cap_mb: int = 0
    progress: tuple[int, int] | None = None  # CP-SAT tree: closed, total
    finished: int = 0
    waiting_until: float = 0.0
    last_reached: float = (
        0.0  # time.time() of the last answer from the server, 0 if none yet
    )
    exact_row: tuple[int, int] | None = None  # an exact decision: (row reached, states)


@dataclass(frozen=True)
class Event:
    kind: str  # "log", "status", "result" or "picture"
    message: str = ""
    status: Status | None = None
    # for "picture": (piece, width, height, border or 0 for a tiling, placements)
    picture: tuple[str, int, int, int, list] | None = None
    hunt: bool = False  # a picture from the symmetric hunt (drawn in its own colours)
    strip: bool = (
        False  # a half-strip filling: border is its depth, height includes the overhang
    )


class ActiveClock:
    """Wall time excluding pauses."""

    def __init__(self) -> None:
        self._accumulated = 0.0
        self._since = time.monotonic()
        self._running = True

    def pause(self) -> None:
        if self._running:
            self._accumulated += time.monotonic() - self._since
            self._running = False

    def resume(self) -> None:
        if not self._running:
            self._since = time.monotonic()
            self._running = True

    def elapsed(self) -> float:
        return self._accumulated + (
            time.monotonic() - self._since if self._running else 0.0
        )


@dataclass
class SolverRunner:
    """One attempt at a job: the child process, its clock and its guard state. Shared with the watchdog,
    so every access goes through `lock`."""

    job: Job
    cap_mb: int
    tree_progress: bool = False
    exact_dir: Path | None = None  # where an exact job keeps its checkpoint
    keep_checkpoint: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    clock: ActiveClock = field(default_factory=ActiveClock)
    stage_index: int = -1
    stage_started_at: float = 0.0
    peak_mb: int = 0
    used_mb: int = 0
    progress: tuple[int, int] | None = None
    exact_row: tuple[int, int] | None = None
    first_stage: int = 0
    kill_reason: str = ""  # "timeout" or "memory: ..."
    finished: bool = False
    _process: multiprocessing.Process | None = None
    _handle: psutil.Process | None = None
    _queue: object = None
    _job_limit: object = None

    def start(self) -> None:
        if self.job.exact:
            self._queue = queue.Queue()
            files = ExactFiles(self.exact_dir, self.job.piece, self.job.width)
            self._process = ExactProcess(
                piece_by_name(self.job.piece),
                self.job.width,
                files,
                self._queue,
                self.keep_checkpoint,
            )
            self._watch_process()
            return
        context = multiprocessing.get_context("spawn")
        self._queue = context.Queue()
        task = SolverTask(
            self.job.piece,
            self.job.width,
            self.job.height,
            self.job.stages,
            self.job.threads,
            memory_guard.solver_soft_limit_mb(self.cap_mb),
            os.getpid(),
            tree_workers_for(self.job.threads) if self.tree_progress else 0,
            self.first_stage,
        )
        self._process = context.Process(
            target=run_task, args=(task, self._queue), daemon=True
        )
        self._process.start()
        self._watch_process()

    def _watch_process(self) -> None:
        self._handle = psutil.Process(self._process.pid)
        if sys.platform == "win32":
            self._job_limit = memory_guard.WindowsJobLimit(self.cap_mb)
            self._job_limit.assign(self._process.pid)
        memory_guard.lower_priority(self._handle)

    def drain(self) -> list[tuple]:
        messages = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                return messages

    def alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def exit_code(self) -> int | None:
        return self._process.exitcode if self._process else None

    def stage_time(self) -> float:
        return (
            self.clock.elapsed() - self.stage_started_at
            if self.stage_index >= 0
            else 0.0
        )

    def suspend(self) -> None:
        with self.lock:
            if self.alive():
                self._handle.suspend()
            self.clock.pause()

    def resume(self) -> None:
        with self.lock:
            if self.alive():
                self._handle.resume()
            self.clock.resume()

    def guard(self) -> None:
        """Called by the watchdog: kill on memory pressure or when the current stage runs out of time."""
        with self.lock:
            if self.finished or self.kill_reason or not self.alive():
                return
            try:
                verdict = memory_guard.check(self._handle, self.cap_mb)
            except psutil.NoSuchProcess:
                return
            if verdict.used_mb is not None:
                self.used_mb = verdict.used_mb
                self.peak_mb = max(self.peak_mb, verdict.used_mb)
            if verdict.kill:
                self._kill_locked("memory: " + verdict.reason)
                return
            stage = self.job.stages[self.stage_index] if self.stage_index >= 0 else None
            if (
                stage is not None
                and not stage.unlimited
                and self.stage_time() >= stage.time_limit_s
            ):
                self._kill_locked("timeout")

    def kill(self) -> None:
        with self.lock:
            self._kill_locked(self.kill_reason or "cancelled")

    def _kill_locked(self, reason: str) -> None:
        self.kill_reason = reason
        self.clock.pause()
        if self._process is not None and self._process.is_alive():
            try:
                self._handle.kill()  # works on suspended processes too
            except psutil.Error:
                # it may exit on its own between the check and the kill; fall back to the OS handle
                if self._process.is_alive():
                    self._process.kill()
            self._process.join(KILL_JOIN_S)
            if self._process.is_alive():
                raise RuntimeError(
                    f"could not stop the solver process {self._process.pid}"
                )
        if self._job_limit is not None:
            self._job_limit.close()
            self._job_limit = None


class Controller:
    def __init__(
        self,
        directory: Path,
        key: bytes,
        on_event: Callable[[Event], None],
        label: str = "",
    ) -> None:
        self._directory = directory
        self._prefix = f"[{label}] " if label else ""
        attach_log_file(directory)
        self._key = key
        self._on_event = on_event
        self._pending = PendingResults(directory)
        self._commands: queue.Queue = queue.Queue()
        self._state = State.IDLE
        self._settings: Settings | None = None
        self._requested: tuple[int, int | None] | None = None
        self._widths: WidthQueue | None = None
        self._api: ApiClient | None = None
        self._job: Job | None = None
        self._filled_stage = (
            -1
        )  # deepest stage of the current job whose border was filled
        self._unsent_frame: tuple[int, list] | None = (
            None  # (stage index, placements) not yet saved
        )
        self._resume_skipped: set[int] = (
            set()
        )  # checkpointed widths the server would not give back this session
        self._runner: SolverRunner | None = None
        self._runner_lock = threading.Lock()
        self._finished = 0
        self._memory_failures = 0
        self._crashes = 0
        self._next_request_at = 0.0
        self._next_heartbeat_at = 0.0
        self._next_pending_at = 0.0
        self._next_status_at = 0.0
        self._stopping = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="controller", daemon=True
        )
        self._watchdog = threading.Thread(
            target=self._watch, name="watchdog", daemon=True
        )

    def launch(self) -> None:
        self._worker.start()
        self._watchdog.start()

    # commands, safe to call from any thread
    def start(
        self,
        settings: Settings,
        requested: tuple[int, int | None] | None = None,
        widths: WidthQueue | None = None,
    ) -> None:
        """widths: the volunteer's own strip widths, shared by the parallel searches (width search only)."""
        self._commands.put(("start", settings, requested, widths))

    def pause(self) -> None:
        self._commands.put(("pause",))

    def resume(self) -> None:
        self._commands.put(("resume",))

    def stop(self) -> None:
        self._commands.put(("stop",))

    def give_up(self) -> None:
        self._commands.put(("give_up",))

    def shutdown(self, timeout_s: float = 15.0) -> None:
        self._commands.put(("shutdown",))
        self._worker.join(timeout_s)
        self._stopping.set()

    # worker thread
    def _run(self) -> None:
        self._log(f"fe70 {__version__} ready.")
        while True:
            try:
                command = self._commands.get(timeout=LOOP_S)
            except queue.Empty:
                command = None
            if command and command[0] == "shutdown":
                self._close()
                return
            try:
                if command:
                    self._handle_command(command)
                self._step()
            except Exception as error:  # noqa: BLE001 - logged to error.log and the window
                self._internal_error(error)

    def _internal_error(self, error: Exception) -> None:
        """A bug: record the traceback, stop the solver and go idle instead of freezing the window."""
        details = "".join(traceback.format_exception(error))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (self._directory / "error.log").open("a", encoding="utf-8") as log_file:
            log_file.write(f"{stamp}\n{details}\n")
        self._log(
            f"Internal error: {error}. Details are in {self._directory / 'error.log'}."
        )
        self._drop_runner()
        self._state = State.IDLE

    def _handle_command(self, command: tuple) -> None:
        name = command[0]
        if name == "start":
            if self._state == State.PAUSED:
                self._handle_command(("resume",))
                return
            if self._state == State.RUNNING:
                return
            _, settings, requested, widths = command
            if self._state == State.IDLE:
                self._settings, self._requested = settings, requested
                self._widths = widths
                self._resume_skipped = set()
                self._api = ApiClient(settings.server_url, settings.access_token)
                self._next_request_at = 0.0
                self._memory_failures = 0
                self._crashes = 0
            self._state = State.RUNNING
            if self._job is None:
                self._log("Started.")
            elif self._job.exact and self._settings.save_checkpoints:
                self._log("Continuing the exact decision from its last checkpoint.")
            else:
                self._log("Restarting the current size from the beginning.")
        elif name == "pause" and self._state == State.RUNNING:
            # also valid between sizes or just as a search finishes: no new size is fetched while paused
            if self._runner:
                self._runner.suspend()
            self._state = State.PAUSED
            self._log(
                "Paused. The search is frozen in memory; Resume continues where it left off."
            )
        elif name == "resume" and self._state == State.PAUSED:
            if self._runner:
                self._runner.resume()
            self._state = State.RUNNING
            self._log("Resumed.")
        elif name == "stop" and self._state in (State.RUNNING, State.PAUSED):
            self._drop_runner()
            self._state = State.STOPPED if self._job else State.IDLE
            if self._job:
                self._log(
                    "Stopped and memory freed. This size stays reserved for you; Start runs it again."
                )
            else:
                self._log("Stopped.")
        elif name == "give_up" and self._job is not None:
            self._drop_runner()
            self._release(RELEASE_GAVE_UP, "")
            self._state = State.IDLE
            self._log("Gave up; the size goes back to the server for someone else.")
        self._emit_status(force=True)

    def _step(self) -> None:
        now = time.monotonic()
        if self._runner is not None:
            self._poll_runner()
        if self._state == State.RUNNING and self._runner is None:
            if self._job is None and now >= self._next_request_at:
                self._request_job()
            if self._job is not None and self._state == State.RUNNING:
                self._start_runner()
        if self._job is not None and now >= self._next_heartbeat_at:
            self._heartbeat()
        if now >= self._next_pending_at:
            self._flush_pending()
        self._emit_status()

    def _request_job(self) -> None:
        settings = self._settings
        width, height = self._requested or (None, None)
        queued = resumed = None
        if width is None and settings.search == SEARCH_EXACT:
            resumed = width = self._checkpoint_to_resume()
        if width is None and self._widths is not None:
            queued = width = self._widths.claim(self)
        try:
            self._job = self._api.request_job(
                settings.name,
                settings.max_ram_mb,
                settings.max_threads,
                width,
                height,
                settings.no_time_limit,
                settings.search,
                settings.custom_borders,
                settings.time_limit_s,
            )
        except NoJobAvailable as error:
            self._log(
                f"No size available right now ({error}). Asking again in {NO_JOB_RETRY_S / 60:.0f} min."
            )
            self._next_request_at = time.monotonic() + NO_JOB_RETRY_S
            self._requested = None
            return
        except ServerError as error:
            if error.status in RETRYABLE_STATUSES:
                # a requested size is kept: the retry asks for it again
                self._log(f"{error}. Retrying in {UNREACHABLE_RETRY_S:.0f} s.")
                self._next_request_at = time.monotonic() + UNREACHABLE_RETRY_S
            elif resumed is not None:
                self._resume_skipped.add(resumed)
                if error.status == GONE:
                    self._drop_checkpoints(resumed)
                    self._log(
                        f"Width {resumed} was decided meanwhile ({error}); its checkpoint is deleted."
                    )
                else:
                    self._log(f"Cannot resume width {resumed} now: {error}")
            elif queued is not None:
                self._widths.drop(self)
                self._log(f"Skipping width {queued} from your list: {error}")
            else:
                self._log(f"The server refused the request: {error}")
                self._state = State.IDLE
                self._requested = None
            return
        self._requested = None
        self._filled_stage = -1
        self._unsent_frame = None
        job = self._job
        self._next_heartbeat_at = time.monotonic() + job.heartbeat_interval_s
        stages = ", ".join(s.describe() for s in job.stages)
        if settings.symmetric_hunt and not job.hunt:
            self._log(
                "This server does not run the symmetric hunt yet, so this is an ordinary size."
            )
        if settings.search == SEARCH_FOREVER and not job.periodic:
            self._log(
                "This server does not look for repeating fillings yet, so this is another kind of job."
            )
        if settings.search == SEARCH_EXACT and not job.exact:
            self._log(
                "This server does not hand out exact width decisions yet, so this is another kind of job."
            )
        if job.exact:
            files = ExactFiles(self._exact_dir(), job.piece, job.width)
            keep = settings.save_checkpoints
            again = (
                "resuming from its checkpoint"
                if keep and files.checkpoint.exists()
                else "from the first row"
            )
            saving = (
                f"a checkpoint is saved after every row in {files.directory}"
                if keep
                else "no checkpoints are saved (Save checkpoints is off), so a stop or crash starts it over"
            )
            self._log(
                f"Got width {job.width} for an exact decision ({again}; about "
                f"{job.est_ram_mb / 1024:.0f} GB of RAM, one thread). It runs until the width dies, tiles a "
                f"rectangle or repeats; {saving}."
            )
            return
        if job.periodic:
            self._log(
                f"Got width {job.width} ({job.threads} threads, about {job.est_ram_mb / 1024:.1f} GB). "
                f"Trying: {stages}. A repeating filling proves width {job.width} fills forever."
            )
            return
        if settings.search == SEARCH_WIDTH and not job.strip:
            self._log(
                "This server does not hand out strip widths yet, so this is a rectangle size."
            )
        if job.strip:
            self._log(
                f"Got strip width {job.width} ({job.threads} threads, about {job.est_ram_mb / 1024:.1f} GB). "
                f"Stages: {stages}. If a depth cannot be filled, no rectangle has a side {job.width}."
            )
            return
        if job.filled_border:
            self._log(
                f"An earlier search of {job.width} x {job.height} already filled border depth "
                f"{job.filled_border}, so this one starts deeper."
            )
        self._log(
            f"Got {job.width} x {job.height} ({job.lane} lane), {job.threads} threads, about "
            f"{job.est_ram_mb / 1024:.1f} GB. Stages: {stages}."
        )
        if job.threads < settings.max_threads:
            self._log(
                f"Using {job.threads} of {settings.max_threads} threads, to stay within this search's "
                f"{settings.max_ram_mb / 1024:.1f} GB RAM share (bigger sizes need more memory per "
                f"thread). Raise Max RAM to allow more threads."
            )

    def _exact_dir(self) -> Path:
        return self._settings.exact_dir(self._directory)

    def _checkpoint_to_resume(self) -> int | None:
        if not self._settings.save_checkpoints:
            return None
        for width in checkpointed_widths(self._exact_dir()):
            if width not in self._resume_skipped:
                return width
        return None

    def _drop_checkpoints(self, width: int) -> None:
        for path in self._exact_dir().glob(f"*-{width}.ckpt"):
            piece = path.name.split("-")[0]
            ExactFiles(self._exact_dir(), piece, width).remove()

    def _start_runner(self) -> None:
        if self._job.exact and not self._exact_ready():
            self._state = State.STOPPED
            return
        available = memory_guard.available_ram_mb()
        cap = self._settings.max_ram_mb
        if available < min(cap, self._job.est_ram_mb) + memory_guard.SYSTEM_FLOOR_MB:
            self._log(
                f"Only {available} MB free on this computer, not enough to start safely. Close some programs, "
                f"lower Max RAM, or press Give Up."
            )
            self._state = State.STOPPED
            return
        runner = SolverRunner(
            self._job,
            cap,
            self._settings.tree_progress,
            exact_dir=self._exact_dir(),
            keep_checkpoint=self._settings.save_checkpoints,
            first_stage=self._filled_stage + 1,
        )
        if self._filled_stage >= 0:
            self._log(
                f"Continuing after {self._job.stages[self._filled_stage].describe()}, which was "
                f"already filled."
            )
        runner.start()
        with self._runner_lock:
            self._runner = runner

    def _exact_ready(self) -> bool:
        directory = self._exact_dir()
        directory.mkdir(parents=True, exist_ok=True)
        free_mb = shutil.disk_usage(directory).free // (1 << 20)
        keep = self._settings.save_checkpoints
        need_mb = MIN_FREE_DISK_MB if keep else MIN_FREE_SCRATCH_MB
        if free_mb < need_mb:
            what = "its checkpoint and scratch row" if keep else "its scratch row"
            self._log(
                f"Only {free_mb / 1024:.0f} GB free in {directory}; an exact search needs about "
                f"{need_mb // 1024} GB for {what}. Free some space or pick another Exact folder, then press "
                f"Start."
            )
            return False
        try:
            from tilingvol.client.exact_runner import exact_binary

            exact_binary()
        except ExactBinaryMissing as error:
            self._log(f"Cannot run exact searches: {error}")
            return False
        return True

    def _poll_runner(self) -> None:
        runner = self._runner
        for message in runner.drain():
            kind = message[0]
            if kind == MSG_PROGRESS:
                if message[1] == runner.stage_index:
                    runner.progress = (message[2], message[3])
                continue
            if kind == MSG_EXACT_ROW:
                _, row, states, seconds = message
                runner.exact_row = (row, states)
                self._log(
                    f"Width {self._job.width}: row {row}, {states:,} states "
                    f"({format_duration(seconds)} of compute so far)."
                )
                continue
            if kind == MSG_EXACT_DONE:
                outcome: ExactOutcome = message[1]
                self._finish(outcome.verdict, 0, None, outcome)
                return
            if kind == MSG_STAGE_START:
                with runner.lock:
                    runner.progress = None
                    runner.stage_index = message[1]
                    runner.stage_started_at = runner.clock.elapsed()
                stage = self._job.stages[message[1]]
                self._log(
                    f"Stage {message[1] + 1}/{len(self._job.stages)}: {stage.describe()} "
                    f"(limit: {stage.describe_limit()})."
                )
            elif kind == MSG_FRAME_FEASIBLE:
                job = self._job
                stage = job.stages[message[1]]
                self._filled_stage = message[1]
                self._unsent_frame = (message[1], message[2])
                if job.strip:
                    self._log(
                        f"Width {job.width}: {stage.describe()} can be filled, so the strip lives at "
                        f"least {stage.strip_rows} rows; going deeper."
                    )
                    reach = piece_by_name(job.piece).strip_reach
                    picture = (
                        job.piece,
                        job.width,
                        stage.strip_rows + reach,
                        stage.strip_rows,
                        message[2],
                    )
                else:
                    self._log(
                        f"A {stage.describe()} can be filled, so it proves nothing; going deeper."
                    )
                    picture = (
                        job.piece,
                        job.width,
                        job.height,
                        stage.border,
                        message[2],
                    )
                self._send_frame(job)
                self._on_event(
                    Event("picture", picture=picture, hunt=job.hunt, strip=job.strip)
                )
            elif kind == MSG_IMPOSSIBLE:
                self._finish(VERDICT_IMPOSSIBLE, message[1], None)
                return
            elif kind == MSG_POSSIBLE:
                self._finish(VERDICT_POSSIBLE, message[1], message[2])
                return
            elif kind == MSG_FILLED:
                self._finish(VERDICT_FILLED, message[1], None)
                return
            elif kind == MSG_STOPPED:
                self._memory_failure(
                    f"CP-SAT stopped at its memory limit "
                    f"({memory_guard.solver_soft_limit_mb(runner.cap_mb)} MB)"
                )
                return
            elif kind == MSG_ERROR:
                self._log(f"Solver error: {message[1]}")
                self._drop_runner()
                self._release(RELEASE_ERROR, message[1])
                self._state = State.IDLE
                return
        if runner.kill_reason == "timeout":
            self._finish(VERDICT_TIMED_OUT, runner.stage_index, None)
        elif runner.kill_reason.startswith("memory"):
            self._memory_failure(runner.kill_reason.removeprefix("memory: "))
        elif not runner.alive() and runner.exit_code() is not None:
            # a final message may arrive just before exit
            for message in runner.drain():
                if message[0] == MSG_EXACT_DONE:
                    self._finish(message[1].verdict, 0, None, message[1])
                    return
                if message[0] in (MSG_IMPOSSIBLE, MSG_POSSIBLE, MSG_FILLED):
                    solution = message[2] if len(message) > 2 else None
                    self._finish(message[0], message[1], solution)
                    return
            code = runner.exit_code()
            if runner.peak_mb >= CRASH_AS_MEMORY_FRACTION * runner.cap_mb:
                self._memory_failure(
                    f"the solver died near the RAM limit (code {code})"
                )
            else:
                self._crash(f"the solver crashed (exit code {code:#x})")

    def _crash(self, reason: str) -> None:
        self._drop_runner()
        self._log(f"Error: {reason}. This size goes back to the server.")
        self._release(RELEASE_ERROR, reason)
        self._crashes += 1
        if self._crashes >= MAX_CRASHES:
            self._state = State.IDLE
            self._log(
                f"The solver crashed {MAX_CRASHES} times in a row; stopping. Please report client.log "
                f"from {self._directory}."
            )

    def _finish(
        self,
        verdict: str,
        stage_index: int,
        solution: list | None,
        exact: ExactOutcome | None = None,
    ) -> None:
        runner = self._runner
        duration = runner.clock.elapsed()
        peak = runner.peak_mb
        job = self._job
        self._drop_runner()
        self._memory_failures = 0
        self._crashes = 0
        salt = token.new_salt()
        name = self._settings.name
        payload = token.signing_payload(
            job.job_id,
            job.nonce,
            name,
            job.width,
            job.height,
            verdict,
            stage_index,
            duration,
            salt,
            solution,
            exact.to_dict() if exact else None,
        )
        # a border filling already saved on the server need not travel again
        frame = self._unsent_frame
        self._unsent_frame = None
        submission = ResultSubmission(
            job.job_id,
            job.nonce,
            name,
            verdict,
            stage_index,
            round(duration, 1),
            peak,
            salt,
            token.sign(self._key, payload),
            __version__,
            solution,
            frame_border=(
                job.stages[frame[0]].strip_rows or job.stages[frame[0]].border
                if frame
                else None
            ),
            frame_placements=frame[1] if frame else None,
            exact=exact.to_dict() if exact else None,
        )
        stage = job.stages[stage_index]
        if solution is not None and job.periodic:
            # border 0 on a strip picture: a repeating filling, drawn with two more copies of its block
            rows = (
                stage.periodic_start
                + 3 * stage.period
                + piece_by_name(job.piece).strip_reach
            )
            self._on_event(
                Event(
                    "picture",
                    picture=(job.piece, job.width, rows, 0, unroll(solution, stage)),
                    strip=True,
                )
            )
        elif solution is not None:
            self._on_event(
                Event(
                    "picture",
                    picture=(job.piece, job.width, job.height, 0, solution),
                    hunt=job.hunt,
                )
            )
        self._pending.add(submission.to_dict())
        if exact is not None:
            ExactFiles(self._exact_dir(), job.piece, job.width).remove()
        if exact is not None and verdict == VERDICT_IMPOSSIBLE:
            text = (
                f"Width {job.width} is RULED OUT exactly: no filling of {exact.row} rows exists, so no "
                f"rectangle has a side {job.width}"
            )
        elif exact is not None and verdict == VERDICT_POSSIBLE:
            text = (
                f"Width {job.width}: A {job.width} x {exact.row} RECTANGLE TILES (the flat frontier came "
                f"back at row {exact.row})! This is being sent to the server"
            )
        elif exact is not None:
            text = (
                f"Width {job.width} is RULED OUT: row {exact.row} repeats row {exact.repeat_from} exactly and "
                f"no row was flat, so its half-strip fills forever but no rectangle has a side {job.width}"
            )
        elif job.periodic and verdict == VERDICT_POSSIBLE:
            text = (
                f"Width {job.width} FILLS FOREVER: a filling repeating every {stage.period} rows from row "
                f"{stage.periodic_start} was found and is being sent"
            )
        elif job.periodic and verdict == VERDICT_IMPOSSIBLE:
            text = f"Width {job.width}: no repeating filling with these settings (this proves nothing)"
        elif job.periodic:
            text = f"Width {job.width} timed out at {stage.describe()}"
        elif job.strip and verdict == VERDICT_IMPOSSIBLE:
            text = (
                f"Width {job.width} is RULED OUT: the first {stage.strip_rows} rows of its half-strip "
                f"cannot be covered, so no rectangle has a side {job.width}"
            )
        elif job.strip and verdict == VERDICT_FILLED:
            text = f"Width {job.width}: every depth up to {stage.strip_rows} rows was filled"
        elif job.strip:
            text = f"Width {job.width} timed out at {stage.describe()}"
        elif verdict == VERDICT_IMPOSSIBLE and job.hunt:
            text = (
                f"{job.width} x {job.height} has no symmetric tiling (proved at {stage.describe()}); "
                f"an asymmetric one is still possible"
            )
        elif verdict == VERDICT_IMPOSSIBLE:
            text = f"{job.width} x {job.height} is IMPOSSIBLE (proved at {stage.describe()})"
        elif verdict == VERDICT_POSSIBLE:
            text = f"{job.width} x {job.height}: A TILING WAS FOUND! The solution is being sent to the server"
        else:
            text = f"{job.width} x {job.height} timed out at {stage.describe()}"
        self._finished += 1
        if exact is not None:
            duration = exact.compute_s  # resumed runs included
        message = f"{self._prefix}{text}, after {format_duration(duration)}."
        log.info(message)
        self._on_event(Event("result", message))
        if self._widths is not None and self._widths.holds(self, job.width):
            if verdict == VERDICT_FILLED:
                self._log(f"Width {job.width} from your list: going on deeper.")
            else:
                self._widths.drop(self)
        self._job = None
        self._flush_pending()

    def _memory_failure(self, reason: str) -> None:
        peak = self._runner.peak_mb if self._runner else 0
        self._drop_runner()
        self._log(f"Out of memory: {reason}. This size goes back to the server.")
        self._release(RELEASE_MEMORY, reason, peak)
        self._memory_failures += 1
        if self._memory_failures >= MAX_MEMORY_FAILURES:
            self._state = State.IDLE
            self._log(
                f"{MAX_MEMORY_FAILURES} sizes in a row ran out of memory. Raise Max RAM or lower CPU threads, "
                f"then press Start."
            )

    def _drop_runner(self) -> None:
        with self._runner_lock:
            runner, self._runner = self._runner, None
        if runner is not None:
            runner.finished = True
            runner.kill()

    def _send_frame(self, job: Job) -> None:
        """Save the latest border filling on the server now, so a later search of this size can skip it
        even if this one is stopped. Kept and retried with the heartbeats while the server is away."""
        if self._unsent_frame is None:
            return
        stage_index, placements = self._unsent_frame
        try:
            self._api.send_frame(job, stage_index, placements)
        except ServerError as error:
            if error.status in RETRYABLE_STATUSES:
                self._log(
                    f"Could not save the border filling yet ({error}); will retry."
                )
                return
            self._log(f"The server did not take the border filling: {error}")
        self._unsent_frame = None

    def _release(self, reason: str, detail: str, peak: int = 0) -> None:
        job, self._job = self._job, None
        if job is None:
            return
        if self._widths is not None and self._widths.holds(self, job.width):
            # given back or too big for this computer: leave it; otherwise someone may retry it later
            if reason in (RELEASE_GAVE_UP, RELEASE_MEMORY):
                self._widths.drop(self)
            else:
                self._widths.release(self)
        self._send_frame(job)
        self._unsent_frame = None
        try:
            self._api.release(job, reason, peak, detail[:200])
        except ServerError as error:
            self._log(
                f"Could not tell the server ({error}); the size returns to the pool when its lease expires."
            )

    def _heartbeat(self) -> None:
        job = self._job
        self._send_frame(job)
        self._next_heartbeat_at = time.monotonic() + job.heartbeat_interval_s
        runner = self._runner
        try:
            reply = self._api.heartbeat(
                job,
                self._state.value,
                runner.stage_index if runner else -1,
                runner.clock.elapsed() if runner else 0.0,
                runner.exact_row if runner else None,
                runner.keep_checkpoint if runner else False,
            )
        except ServerError as error:
            if error.status in (404, 409, 410):
                self._log(f"The server cancelled this size: {error}")
                self._drop_runner()
                self._forget_width(job)
                self._job = None
            elif error.status == 0:
                self._log(
                    f"{error}. The search continues; the result is kept until the server is back."
                )
            return
        if reply.get("cancel"):
            self._log(f"The server cancelled this size: {reply.get('reason', '')}")
            self._drop_runner()
            self._forget_width(job)
            self._job = None

    def _forget_width(self, job: Job) -> None:
        if self._widths is not None and self._widths.holds(self, job.width):
            self._widths.drop(self)

    def _flush_pending(self) -> None:
        self._next_pending_at = time.monotonic() + PENDING_RETRY_S
        if self._api is None:
            return
        # parallel searches share the file; holding the lock keeps two of them from sending the same result
        with self._pending.lock:
            for item in self._pending.load():
                try:
                    reply = self._api.submit(ResultSubmission.from_dict(item))
                except ServerError as error:
                    if error.status in RETRYABLE_STATUSES:
                        self._log(f"Result kept locally, will retry: {error}")
                        return
                    self._log(f"The server rejected a result: {error}")
                    self._pending.remove(item["salt"])
                    continue
                self._pending.remove(item["salt"])
                message = reply.get("message", "")
                self._log(f"Result accepted by the server. {message}".strip())

    def _close(self) -> None:
        if self._job is not None:
            self._drop_runner()
            self._release(RELEASE_CLOSED, "")
        self._state = State.IDLE

    def _watch(self) -> None:
        while not self._stopping.is_set():
            with self._runner_lock:
                runner = self._runner
            if runner is not None:
                runner.guard()
            time.sleep(WATCHDOG_S)

    def _log(self, message: str) -> None:
        log.info(self._prefix + message)
        self._on_event(Event("log", self._prefix + message))

    def _emit_status(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_status_at:
            return
        self._next_status_at = now + STATUS_S
        runner = self._runner
        self._on_event(
            Event(
                "status",
                status=Status(
                    state=self._state,
                    job=self._job,
                    stage_index=runner.stage_index if runner else -1,
                    stage_active_s=runner.stage_time() if runner else 0.0,
                    total_active_s=runner.clock.elapsed() if runner else 0.0,
                    ram_used_mb=runner.used_mb if runner else 0,
                    ram_cap_mb=self._settings.max_ram_mb if self._settings else 0,
                    progress=runner.progress if runner else None,
                    exact_row=runner.exact_row if runner else None,
                    finished=self._finished,
                    waiting_until=self._next_request_at if self._job is None else 0.0,
                    last_reached=self._api.last_reached if self._api else 0.0,
                ),
            )
        )


def tree_workers_for(threads: int) -> int:
    """Half the workers do tree search when progress is wanted; CP-SAT needs at least two of them."""
    return threads // 2 if threads >= MIN_TREE_THREADS else 0


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def attach_log_file(directory: Path) -> None:
    """Everything shown in the window also goes to client.log, for bug reports."""
    path = str(directory / "client.log")
    if any(getattr(h, "baseFilename", None) == path for h in log.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_FILE_BYTES, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
