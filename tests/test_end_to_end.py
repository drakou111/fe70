"""Real server (uvicorn on localhost), real controller, real solver child processes."""

import sys
import threading
import time

import pytest

from tests.conftest import wait_for
from tilingvol.client.controller import Controller, SolverRunner, State
from tilingvol.client.settings import PendingResults, Settings
from tilingvol.shared.pieces import GUN
from tilingvol.shared.protocol import Job, Stage

NAME = "e2e"


class Recorder:
    def __init__(self) -> None:
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event) -> None:
        with self.lock:
            self.events.append(event)

    def messages(self) -> list[str]:
        with self.lock:
            return [e.message for e in self.events if e.kind in ("log", "result")]

    def last_status(self):
        with self.lock:
            statuses = [e.status for e in self.events if e.kind == "status"]
        return statuses[-1] if statuses else None

    def has(self, text: str) -> bool:
        return any(text in m for m in self.messages())


@pytest.fixture
def controller(tmp_path, submit_key):
    recorder = Recorder()
    c = Controller(tmp_path, submit_key, recorder)
    c.launch()
    yield c, recorder
    c.shutdown()


def settings(url: str, ram_mb: int = 4096, threads: int = 2) -> Settings:
    return Settings(
        name=NAME, max_ram_mb=ram_mb, max_threads=threads, server_url=url
    ).validated()


def results(server) -> list:
    return [r for r in server.db.results() if r.source == "volunteer"]


def test_runs_jobs_continuously_and_reports_impossible(live_server, controller):
    c, recorder = controller
    c.start(settings(live_server.url))
    wait_for(lambda: len(results(live_server)) >= 3, 120)
    c.stop()
    rows = results(live_server)
    assert all(r.verdict == "impossible" and r.name == NAME for r in rows)
    assert rows[0].width * rows[0].height <= rows[1].width * rows[1].height
    assert recorder.has("IMPOSSIBLE") and recorder.has("Result accepted")


def test_finds_and_uploads_a_tiling(live_server, controller):
    c, recorder = controller
    c.start(settings(live_server.url), requested=(19, 28))
    wait_for(lambda: any(r.verdict == "possible" for r in results(live_server)), 120)
    c.stop()
    assert live_server.db.solution(19, 28) is not None
    assert recorder.has("A TILING WAS FOUND")


def test_timeout_excludes_pauses(sib_config, make_live_server, controller):
    server = make_live_server(sib_config, piece=GUN, stages=(Stage(0, False, 4.0),))
    c, recorder = controller
    c.start(settings(server.url, ram_mb=8192, threads=1), requested=(140, 150))
    wait_for(lambda: (s := recorder.last_status()) and s.stage_index == 0, 60)
    c.pause()
    wait_for(lambda: recorder.last_status().state == State.PAUSED, 10)
    time.sleep(6)  # longer than the stage limit; must not count
    assert not results(server)
    c.resume()
    wait_for(lambda: results(server), 60)
    c.stop()
    row = results(server)[0]
    assert row.verdict == "timed_out" and 3.8 <= row.duration_s < 6.0


def test_stop_keeps_the_size_and_give_up_releases_it(live_server, controller):
    c, recorder = controller
    c.start(settings(live_server.url), requested=(35, 35))
    wait_for(
        lambda: (
            (s := recorder.last_status()) and s.job is not None and s.stage_index >= 0
        ),
        60,
    )
    c.stop()
    wait_for(lambda: recorder.last_status().state == State.STOPPED, 10)
    held = recorder.last_status().job
    active = live_server.db.active_jobs()
    assert [(j.width, j.height) for j in active] == [(35, 35)]
    c.give_up()
    wait_for(lambda: recorder.last_status().state == State.IDLE, 10)
    assert held.width == 35
    assert not live_server.db.active_jobs()


def test_results_survive_a_server_outage(
    sib_config, make_live_server, tmp_path, submit_key
):
    server = make_live_server(sib_config)
    recorder = Recorder()
    c = Controller(tmp_path, submit_key, recorder)
    c.launch()
    c.start(settings(server.url))
    wait_for(
        lambda: recorder.last_status() and recorder.last_status().stage_index >= 0, 60
    )
    server.__exit__()  # the server goes away mid-job
    wait_for(lambda: PendingResults(tmp_path).load(), 120)
    c.stop()
    c.shutdown()
    assert recorder.has("Result kept locally")
    revived = make_live_server(sib_config)  # same database, new port
    recorder2 = Recorder()
    c2 = Controller(tmp_path, submit_key, recorder2)
    c2.launch()
    c2.start(settings(revived.url))
    wait_for(lambda: not PendingResults(tmp_path).load(), 60)
    c2.stop()
    c2.shutdown()
    assert any(r.source == "volunteer" for r in revived.db.results())


def test_memory_guard_kills_the_solver():
    job = Job(
        "0" * 32,
        "1" * 32,
        "gun",
        200,
        220,
        (Stage(0, False, 600.0),),
        1,
        0,
        "test",
        60.0,
    )
    runner = SolverRunner(job, cap_mb=300)
    runner.start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and runner.alive() and not runner.kill_reason:
        runner.guard()
        time.sleep(0.1)
    killed_by_watchdog = runner.kill_reason.startswith("memory")
    killed_by_os = (
        sys.platform == "win32"
        and not runner.alive()
        and runner.exit_code() not in (0, None)
    )
    runner.kill()
    assert killed_by_watchdog or killed_by_os
    assert runner.peak_mb < 300
