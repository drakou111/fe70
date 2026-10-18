"""Entry point of the solver child process. It runs the job's stages in order and reports through a queue;
the parent owns the clock (stage time limits exclude pauses), so CP-SAT itself has no time limit here."""

import os
import threading
import time
from dataclasses import dataclass

PARENT_POLL_S = 2.0

MSG_STAGE_START = "stage_start"
MSG_FRAME_FEASIBLE = "frame_feasible"
MSG_IMPOSSIBLE = "impossible"
MSG_POSSIBLE = "possible"
MSG_STOPPED = "stopped"  # CP-SAT gave up on its own: its memory limit was reached
MSG_ERROR = "error"


@dataclass(frozen=True)
class SolverTask:
    piece: str
    width: int
    height: int
    stages: tuple  # tuple[Stage, ...]
    threads: int
    memory_limit_mb: int
    parent_pid: int


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


def _run_stages(task: SolverTask, queue) -> None:
    from ortools.sat.python import cp_model

    from tilingvol.client.model import build_model
    from tilingvol.shared.pieces import piece_by_name

    piece = piece_by_name(task.piece)
    for index, stage in enumerate(task.stages):
        queue.put((MSG_STAGE_START, index))
        built = build_model(
            piece, task.width, task.height, stage.border, stage.lookahead
        )
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = task.threads
        solver.parameters.max_memory_in_mb = task.memory_limit_mb
        status = solver.Solve(built.model)
        if status == cp_model.INFEASIBLE:
            queue.put((MSG_IMPOSSIBLE, index))
            return
        if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            queue.put((MSG_STOPPED, index))
            return
        if stage.border == 0:
            placements = [
                list(built.placement_of[cells])
                for cells, v in built.variables.items()
                if solver.Value(v)
            ]
            queue.put((MSG_POSSIBLE, index, placements))
            return
        queue.put((MSG_FRAME_FEASIBLE, index))
    queue.put(
        (
            MSG_ERROR,
            "every stage was a feasible frame; the last stage must be the full model",
        )
    )
