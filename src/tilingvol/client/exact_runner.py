"""Exact width decisions: runs native/exact_width.cpp's program as a child process and turns its output into
the same queue messages the CP-SAT solver process sends. With checkpoints on (Settings.save_checkpoints)
the program saves one after every row, so a crash, a closed window or Stop loses at most one row: the next
run of that width resumes from it. Off, nothing is saved and a stopped run starts over."""

import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from tilingvol.client.solver_process import MSG_ERROR, MSG_STAGE_START
from tilingvol.shared.pieces import PieceSpec
from tilingvol.shared.protocol import ExactOutcome

MSG_EXACT_ROW = "exact_row"  # (row, states, compute seconds so far)
MSG_EXACT_DONE = "exact_done"  # (ExactOutcome)
BINARY_ENV = "TILINGVOL_EXACT_BIN"
BINARY_NAME = "exact_width.exe" if sys.platform == "win32" else "exact_width"
CHECKPOINT_SUFFIX = ".ckpt"
SCRATCH_SUFFIX = ".raw"
LOG_SUFFIX = ".log"
# one raw row of a width near 140 takes up to about 15 GB, a checkpoint as much again; wider ones more
MIN_FREE_DISK_MB = 40 * 1024
MIN_FREE_SCRATCH_MB = 20 * 1024  # without checkpoints
EXIT_FAILED = 2  # the program's own errors (disk full, damaged checkpoint), explained on its last stderr line
CHECKPOINT_NAME = re.compile(r"(?P<piece>[a-z]+)-(?P<width>\d+)\.ckpt")
ROW_LINE = re.compile(r"row (\d+) states (\d+) words \S+ seconds (\d+)")
ROOT = Path(__file__).resolve().parents[3]


class ExactBinaryMissing(RuntimeError):
    pass


def exact_binary() -> Path:
    """The packaged program (PyInstaller), else a development build (scripts/build_client.py builds it into
    build/native). BINARY_ENV points elsewhere, for tests."""
    override = os.environ.get(BINARY_ENV)
    if override:
        candidates = [Path(override)]
    elif getattr(sys, "frozen", False):
        candidates = [Path(sys._MEIPASS) / BINARY_NAME]  # type: ignore[attr-defined]
    else:
        candidates = [ROOT / "build" / "native" / BINARY_NAME]
    for path in candidates:
        if path.is_file():
            return path
    raise ExactBinaryMissing(
        f"the exact search program is missing ({candidates[0]}); "
        f"build it with scripts/build_client.py"
    )


@dataclass(frozen=True)
class ExactFiles:
    directory: Path
    piece: str
    width: int

    @property
    def stem(self) -> str:
        return f"{self.piece}-{self.width}"

    @property
    def checkpoint(self) -> Path:
        return self.directory / (self.stem + CHECKPOINT_SUFFIX)

    @property
    def scratch(self) -> Path:
        return self.directory / (self.stem + SCRATCH_SUFFIX)

    @property
    def log(self) -> Path:
        return self.directory / (self.stem + LOG_SUFFIX)

    def remove(self) -> None:
        """After the result is saved for sending: the checkpoint and scratch row are no longer needed."""
        for path in (
            self.checkpoint,
            self.scratch,
            self.checkpoint.with_suffix(".ckpt.tmp"),
        ):
            path.unlink(missing_ok=True)


def checkpointed_widths(directory: Path) -> list[int]:
    """Widths with a checkpoint to resume, smallest first."""
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        match = CHECKPOINT_NAME.fullmatch(path.name)
        if match:
            found.append(int(match["width"]))
    return sorted(found)


def cells_argument(piece: PieceSpec) -> str:
    return " ".join(f"{x},{y}" for x, y in piece.cells)


def parse_outcome(lines: list[str]) -> ExactOutcome | None:
    """The final 'counts ...' and verdict lines, or None if the run has not ended."""
    if len(lines) < 2 or not lines[-2].startswith("counts "):
        return None
    counts = tuple(int(c) for c in lines[-2].split()[1:])
    words = lines[-1].split()
    seconds = 0.0
    for line in reversed(lines):
        match = ROW_LINE.fullmatch(line)
        if match:
            seconds = float(match[3])
            break
    row = int(words[-1])
    repeat_from = int(words[1]) if words[0] == "forever" else 0
    return ExactOutcome(
        words[0], row, repeat_from, row - len(counts) + 1, counts, seconds
    )


class ExactProcess:
    """The program as a child process, with the interface SolverRunner uses for the CP-SAT process."""

    def __init__(
        self,
        piece: PieceSpec,
        width: int,
        files: ExactFiles,
        messages: queue.Queue,
        keep_checkpoint: bool,
    ) -> None:
        files.directory.mkdir(parents=True, exist_ok=True)
        command = [
            str(exact_binary()),
            "--width",
            str(width),
            "--cells",
            cells_argument(piece),
            "--start-row",
            str(piece.exact_start_row),
            "--scratch",
            str(files.scratch),
        ]
        if keep_checkpoint:
            command += ["--checkpoint", str(files.checkpoint)]
        self._log = files.log.open("a", encoding="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._popen = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
            creationflags=flags,
        )
        self._messages = messages
        self._log_path = files.log
        self._scratch = files.scratch
        self._reader = threading.Thread(
            target=self._read, name="exact-output", daemon=True
        )
        self._reader.start()

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def exitcode(self) -> int | None:
        return self._popen.poll()

    def is_alive(self) -> bool:
        return self._popen.poll() is None

    def kill(self) -> None:
        self._popen.kill()

    def join(self, timeout: float | None = None) -> None:
        try:
            self._popen.wait(timeout)
        except subprocess.TimeoutExpired:
            return
        self._reader.join(timeout)
        self._log.close()
        # a killed run leaves its half-written row (gigabytes); a resumed run redoes that row anyway
        self._scratch.unlink(missing_ok=True)

    def _read(self) -> None:
        lines: list[str] = []
        for raw in self._popen.stdout:
            line = raw.strip()
            lines.append(line)
            if line.startswith("start "):
                self._messages.put((MSG_STAGE_START, 0))
                continue
            match = ROW_LINE.fullmatch(line)
            if match:
                self._messages.put(
                    (MSG_EXACT_ROW, int(match[1]), int(match[2]), float(match[3]))
                )
                continue
            if line.startswith("counts "):
                continue
            try:
                outcome = parse_outcome(lines)
            except (ValueError, IndexError) as error:
                self._messages.put(
                    (MSG_ERROR, f"unreadable exact output {line!r}: {error}")
                )
                return
            if outcome is not None:
                self._messages.put((MSG_EXACT_DONE, outcome))
                return
            self._messages.put((MSG_ERROR, f"unexpected exact output {line!r}"))
            return
        # no verdict: killed, out of memory (the parent tells those apart), or one of its own errors
        if self._popen.wait() == EXIT_FAILED:
            self._log.flush()
            last = self._log_path.read_text(encoding="utf-8").strip().splitlines()
            self._messages.put(
                (MSG_ERROR, last[-1] if last else "the exact search failed")
            )
