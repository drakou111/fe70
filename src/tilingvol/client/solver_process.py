"""Entry point of the solver child process. It runs the job's stages in order and reports through a queue;
the parent owns the clock (stage time limits exclude pauses), so CP-SAT itself has no time limit here."""

import os
import re
import threading
import time
from dataclasses import dataclass

PARENT_POLL_S = 2.0

MSG_STAGE_START = "stage_start"
MSG_FRAME_FEASIBLE = "frame_feasible"
MSG_IMPOSSIBLE = "impossible"
MSG_POSSIBLE = "possible"
MSG_FILLED = "filled"  # a strip job: every depth was filled
MSG_STOPPED = "stopped"  # CP-SAT gave up on its own: its memory limit was reached
MSG_ERROR = "error"
MSG_PROGRESS = (
    "progress"  # CP-SAT's shared-tree "closed X/Y" (only with about 16+ workers)
)
TREE_LINE = re.compile(r"#Tree\s+[\d.]+s closed:(\d+)/(\d+)")
PROGRESS_EVERY_S = 5.0


@dataclass(frozen=True)
class SolverTask:
    piece: str
    width: int
    height: int
    stages: tuple  # tuple[Stage, ...]
    threads: int
    memory_limit_mb: int
    parent_pid: int
    tree_workers: int = (
        0  # > 0: force CP-SAT's shared-tree search, which reports "closed X/Y" progress
    )
    first_stage: int = 0  # the stages before it were already filled (after Stop & free RAM, then Start)


def _exit_when_parent_dies(parent_pid: int) -> None:
    import psutil

    while True:
        if not psutil.pid_exists(parent_pid):
            os._exit(1)
        time.sleep(PARENT_POLL_S)


def run_task(task: SolverTask, queue) -> None:
    threading.Thread(
        target=_exit_when_parent_dies, args=(task.parent_pid,), daemon=True
    ).start()
    try:
        _run_stages(task, queue)
    except Exception as error:
        queue.put((MSG_ERROR, f"{type(error).__name__}: {error}"))
        raise


class TreeProgress:
    """Forwards CP-SAT's tree progress lines, at most every PROGRESS_EVERY_S seconds."""

    def __init__(self, stage_index: int, queue) -> None:
        self.stage_index = stage_index
        self.queue = queue
        self.last_sent = 0.0

    def on_line(self, line: str) -> None:
        match = TREE_LINE.match(line)
        now = time.monotonic()
        if match and now - self.last_sent >= PROGRESS_EVERY_S:
            self.last_sent = now
            self.queue.put(
                (MSG_PROGRESS, self.stage_index, int(match[1]), int(match[2]))
            )


def _run_stages(task: SolverTask, queue) -> None:
    from ortools.sat.python import cp_model

    from tilingvol.client.model import (
        build_model,
        build_periodic_model,
        build_strip_model,
    )
    from tilingvol.shared.pieces import piece_by_name

    piece = piece_by_name(task.piece)
    for index in range(task.first_stage, len(task.stages)):
        stage = task.stages[index]
        queue.put((MSG_STAGE_START, index))
        if stage.period:
            built = build_periodic_model(
                piece, task.width, stage.periodic_start, stage.period
            )
        elif stage.strip_rows:
            built = build_strip_model(piece, task.width, stage.strip_rows)
        else:
            built = build_model(
                piece,
                task.width,
                task.height,
                stage.border,
                stage.lookahead,
                stage.symmetric,
            )
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = task.threads
        solver.parameters.max_memory_in_mb = task.memory_limit_mb
        if task.tree_workers:
            solver.parameters.shared_tree_num_workers = task.tree_workers
        solver.parameters.log_search_progress = True
        solver.parameters.log_to_stdout = False
        solver.log_callback = TreeProgress(index, queue).on_line
        status = solver.Solve(built.model)
        if status == cp_model.INFEASIBLE and stage.period:
            del built, solver
            continue  # no repeating filling with this start and period; try the next one
        if status == cp_model.INFEASIBLE:
            queue.put((MSG_IMPOSSIBLE, index))
            return
        if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            queue.put((MSG_STOPPED, index))
            return
        placements = [
            list(built.placement_of[cells])
            for cells, v in built.variables.items()
            if solver.Value(v)
        ]
        # free this stage's model before the next one is built, so two never share the RAM
        del built, solver
        if stage.border == 0 and not stage.strip_rows:
            # a rectangle tiling, or a repeating strip filling (the width fills forever)
            queue.put((MSG_POSSIBLE, index, placements))
            return
        # the filled border goes along: the app shows it, and the server's gallery too
        queue.put((MSG_FRAME_FEASIBLE, index, placements))
    if task.stages[-1].strip_rows:
        queue.put((MSG_FILLED, len(task.stages) - 1))
        return
    if task.stages[-1].period:
        # every start and period of the job: no repeating filling (proves nothing about the width)
        queue.put((MSG_IMPOSSIBLE, len(task.stages) - 1))
        return
    queue.put(
        (
            MSG_ERROR,
            "every stage was a feasible frame; the last stage must be the full model",
        )
    )
