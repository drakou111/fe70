"""Job state machine shared by the GUI and headless runs. One worker thread owns all server traffic and job
state; a watchdog thread enforces the RAM cap and the stage time limits so slow network calls never delay
it. The GUI talks to it only through commands and receives Events."""

import logging
import logging.handlers
import multiprocessing
import os
import queue
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
from tilingvol.client.settings import PendingResults, Settings
from tilingvol.client.solver_process import (
    MSG_ERROR,
    MSG_FRAME_FEASIBLE,
    MSG_IMPOSSIBLE,
    MSG_POSSIBLE,
    MSG_PROGRESS,
    MSG_STAGE_START,
    MSG_STOPPED,
    SolverTask,
    run_task,
)
from tilingvol.shared import token
from tilingvol.shared.protocol import (
    RELEASE_CLOSED,
    RELEASE_ERROR,
    RELEASE_GAVE_UP,
    RELEASE_MEMORY,
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
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

log = logging.getLogger("tilingvol.client")


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


@dataclass(frozen=True)
class Event:
    kind: str  # "log", "status", "result" or "picture"
    message: str = ""
    status: Status | None = None
    # for "picture": (piece, width, height, border or 0 for a tiling, placements)
    picture: tuple[str, int, int, int, list] | None = None


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
    lock: threading.Lock = field(default_factory=threading.Lock)
    clock: ActiveClock = field(default_factory=ActiveClock)
    stage_index: int = -1
    stage_started_at: float = 0.0
    peak_mb: int = 0
    used_mb: int = 0
    progress: tuple[int, int] | None = None
    frame: tuple[int, list] | None = (
        None  # deepest border filled so far: (depth, placements)
    )
    kill_reason: str = ""  # "timeout" or "memory: ..."
    finished: bool = False
    _process: multiprocessing.Process | None = None
    _handle: psutil.Process | None = None
    _queue: object = None
    _job_limit: object = None

    def start(self) -> None:
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
        )
        self._process = context.Process(
            target=run_task, args=(task, self._queue), daemon=True
        )
        self._process.start()
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
            self.used_mb = verdict.used_mb
            self.peak_mb = max(self.peak_mb, verdict.used_mb)
            if verdict.kill:
                self._kill_locked("memory: " + verdict.reason)
                return
            if (
                self.stage_index >= 0
                and self.stage_time() >= self.job.stages[self.stage_index].time_limit_s
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
        self._requested: tuple[int, int] | None = None
        self._api: ApiClient | None = None
        self._job: Job | None = None
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
        self, settings: Settings, requested: tuple[int, int] | None = None
    ) -> None:
        self._commands.put(("start", settings, requested))

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
            _, settings, requested = command
            if self._state == State.IDLE:
                self._settings, self._requested = settings, requested
                self._api = ApiClient(settings.server_url, settings.access_token)
                self._next_request_at = 0.0
                self._memory_failures = 0
                self._crashes = 0
            self._state = State.RUNNING
            self._log(
                "Started."
                if self._job is None
                else "Restarting the current size from the beginning."
            )
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
        try:
            self._job = self._api.request_job(
                settings.name,
                settings.max_ram_mb,
                settings.max_threads,
                width,
                height,
                settings.no_time_limit,
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
                self._log(f"{error}. Retrying in {UNREACHABLE_RETRY_S:.0f} s.")
                self._next_request_at = time.monotonic() + UNREACHABLE_RETRY_S
            else:
                self._log(f"The server refused the request: {error}")
                self._state = State.IDLE
            self._requested = None
            return
        self._requested = None
        job = self._job
        self._next_heartbeat_at = time.monotonic() + job.heartbeat_interval_s
        stages = ", ".join(s.describe() for s in job.stages)
        self._log(
            f"Got {job.width} x {job.height} ({job.lane} lane), {job.threads} threads, about "
            f"{job.est_ram_mb / 1024:.1f} GB. Stages: {stages}."
        )

    def _start_runner(self) -> None:
        available = memory_guard.available_ram_mb()
        cap = self._settings.max_ram_mb
        if available < min(cap, self._job.est_ram_mb) + memory_guard.SYSTEM_FLOOR_MB:
            self._log(
                f"Only {available} MB free on this computer, not enough to start safely. Close some programs, "
                f"lower Max RAM, or press Give Up."
            )
            self._state = State.STOPPED
            return
        runner = SolverRunner(self._job, cap, self._settings.tree_progress)
        runner.start()
        with self._runner_lock:
            self._runner = runner

    def _poll_runner(self) -> None:
        runner = self._runner
        for message in runner.drain():
            kind = message[0]
            if kind == MSG_PROGRESS:
                if message[1] == runner.stage_index:
                    runner.progress = (message[2], message[3])
                continue
            if kind == MSG_STAGE_START:
                with runner.lock:
                    runner.progress = None
                    runner.stage_index = message[1]
                    runner.stage_started_at = runner.clock.elapsed()
                stage = self._job.stages[message[1]]
                self._log(
                    f"Stage {message[1] + 1}/{len(self._job.stages)}: {stage.describe()} "
                    f"(limit {format_duration(stage.time_limit_s)})."
                )
            elif kind == MSG_FRAME_FEASIBLE:
                stage = self._job.stages[message[1]]
                runner.frame = (stage.border, message[2])
                self._log(
                    f"A {stage.describe()} can be filled, so it proves nothing; going deeper."
                )
                job = self._job
                self._on_event(
                    Event(
                        "picture",
                        picture=(
                            job.piece,
                            job.width,
                            job.height,
                            stage.border,
                            message[2],
                        ),
                    )
                )
            elif kind == MSG_IMPOSSIBLE:
                self._finish(VERDICT_IMPOSSIBLE, message[1], None)
                return
            elif kind == MSG_POSSIBLE:
                self._finish(VERDICT_POSSIBLE, message[1], message[2])
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
                if message[0] in (MSG_IMPOSSIBLE, MSG_POSSIBLE):
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

    def _finish(self, verdict: str, stage_index: int, solution: list | None) -> None:
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
        )
        frame = runner.frame
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
            frame_border=frame[0] if frame else None,
            frame_placements=frame[1] if frame else None,
        )
        if solution is not None:
            self._on_event(
                Event(
                    "picture", picture=(job.piece, job.width, job.height, 0, solution)
                )
            )
        self._pending.add(submission.to_dict())
        stage = job.stages[stage_index]
        if verdict == VERDICT_IMPOSSIBLE:
            text = f"{job.width} x {job.height} is IMPOSSIBLE (proved at {stage.describe()})"
        elif verdict == VERDICT_POSSIBLE:
            text = f"{job.width} x {job.height}: A TILING WAS FOUND! The solution is being sent to the server"
        else:
            text = f"{job.width} x {job.height} timed out at {stage.describe()}"
        self._finished += 1
        message = f"{self._prefix}{text}, after {format_duration(duration)}."
        log.info(message)
        self._on_event(Event("result", message))
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

    def _release(self, reason: str, detail: str, peak: int = 0) -> None:
        job, self._job = self._job, None
        if job is None:
            return
        try:
            self._api.release(job, reason, peak, detail[:200])
        except ServerError as error:
            self._log(
                f"Could not tell the server ({error}); the size returns to the pool when its lease expires."
            )

    def _heartbeat(self) -> None:
        job = self._job
        self._next_heartbeat_at = time.monotonic() + job.heartbeat_interval_s
        runner = self._runner
        try:
            reply = self._api.heartbeat(
                job,
                self._state.value,
                runner.stage_index if runner else -1,
                runner.clock.elapsed() if runner else 0.0,
            )
        except ServerError as error:
            if error.status in (404, 409, 410):
                self._log(f"The server cancelled this size: {error}")
                self._drop_runner()
                self._job = None
            elif error.status == 0:
                self._log(
                    f"{error}. The search continues; the result is kept until the server is back."
                )
            return
        if reply.get("cancel"):
            self._log(f"The server cancelled this size: {reply.get('reason', '')}")
            self._drop_runner()
            self._job = None

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
                    finished=self._finished,
                    waiting_until=self._next_request_at if self._job is None else 0.0,
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
