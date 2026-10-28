"""Exact width decisions: the native program (built here with g++), its checkpoints, the lane on the server,
and a real run through the controller."""

import os
import queue
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import wait_for
from tests.test_end_to_end import controller, settings  # noqa: F401 - fixture
from tests.test_server import NAME, post_result
from tilingvol.client import controller as controller_module
from tilingvol.client.exact_runner import (
    BINARY_ENV,
    MSG_EXACT_DONE,
    MSG_EXACT_ROW,
    ExactFiles,
    ExactProcess,
    checkpointed_widths,
    parse_outcome,
)
from tilingvol.client.solver_process import MSG_STAGE_START
from tilingvol.client.width_queue import WidthQueue
from tilingvol.server.app import create_app
from tilingvol.shared import token
from tilingvol.shared.pieces import SIBLING
from tilingvol.shared.protocol import (
    EXACT_DEAD,
    EXACT_FOREVER,
    EXACT_RECTANGLE,
    EXACT_SINCE,
    SEARCH_EXACT,
    ExactOutcome,
)

ROOT = Path(__file__).resolve().parents[1]
# the sibling piece, start row 4: width 10 dies at row 2 (CP-SAT: 1 row fills, 2 do not), width 12's row 14
# repeats row 7, and 19 x 28 is its smallest rectangle of width 19
SIB_DEAD = (10, 2)
SIB_FOREVER = (12, 7, 14)
SIB_RECTANGLE = (19, 28)
SLOW_WIDTH = 24  # lives long enough to be stopped part way
TIMEOUT_S = 60


@pytest.fixture(scope="session")
def exact_binary(tmp_path_factory) -> Path:
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is needed to build native/exact_width.cpp")
    name = "exact_width.exe" if sys.platform == "win32" else "exact_width"
    path = tmp_path_factory.mktemp("native") / name
    static = ["-static"] if sys.platform == "win32" else []
    subprocess.run(
        [
            compiler,
            "-O2",
            "-std=c++20",
            *static,
            "-o",
            str(path),
            str(ROOT / "native" / "exact_width.cpp"),
        ],
        check=True,
    )
    return path


@pytest.fixture
def use_binary(exact_binary, monkeypatch) -> Path:
    monkeypatch.setenv(BINARY_ENV, str(exact_binary))
    return exact_binary


def run_width(
    directory: Path, width: int, stop_after_row: int | None = None, keep: bool = True
) -> list[tuple]:
    """Every message of one run; with stop_after_row, the run is killed once that row is reported."""
    messages: queue.Queue = queue.Queue()
    process = ExactProcess(
        SIBLING, width, ExactFiles(directory, "sib", width), messages, keep
    )
    seen = []
    try:
        while True:
            message = messages.get(timeout=TIMEOUT_S)
            seen.append(message)
            if message[0] == MSG_EXACT_DONE:
                break
            if (
                stop_after_row is not None
                and message[0] == MSG_EXACT_ROW
                and message[1] >= stop_after_row
            ):
                process.kill()
                break
    finally:
        process.join(TIMEOUT_S)
    return seen


def outcome_of(messages: list[tuple]) -> ExactOutcome:
    assert messages[-1][0] == MSG_EXACT_DONE, messages[-3:]
    return messages[-1][1]


def rows_of(messages: list[tuple]) -> dict[int, int]:
    return {m[1]: m[2] for m in messages if m[0] == MSG_EXACT_ROW}


def test_the_program_reaches_all_three_verdicts(use_binary, tmp_path):
    width, row = SIB_DEAD
    dead = outcome_of(run_width(tmp_path, width))
    assert (dead.outcome, dead.row, dead.verdict) == (EXACT_DEAD, row, "impossible")

    width, earlier, later = SIB_FOREVER
    messages = run_width(tmp_path, width)
    forever = outcome_of(messages)
    assert (forever.outcome, forever.repeat_from, forever.row) == (
        EXACT_FOREVER,
        earlier,
        later,
    )
    assert forever.verdict == "filled"
    # one count per row, the same ones the row lines reported
    assert dict(
        zip(range(forever.first_row, forever.row + 1), forever.counts)
    ) == rows_of(messages)

    width, height = SIB_RECTANGLE
    rectangle = outcome_of(run_width(tmp_path, width))
    assert (rectangle.outcome, rectangle.row) == (EXACT_RECTANGLE, height)
    # width 10 died before its first checkpoint; the others keep theirs until the app sends the result
    assert checkpointed_widths(tmp_path) == [SIB_FOREVER[0], width]


def test_without_checkpoints_nothing_is_kept(use_binary, tmp_path):
    width, earlier, later = SIB_FOREVER
    forever = outcome_of(run_width(tmp_path, width, keep=False))
    assert (forever.repeat_from, forever.row) == (earlier, later)
    stopped = run_width(tmp_path, SLOW_WIDTH, stop_after_row=8, keep=False)
    again = run_width(tmp_path, SLOW_WIDTH, stop_after_row=6, keep=False)
    assert min(rows_of(stopped)) == min(rows_of(again))  # starts over
    assert checkpointed_widths(tmp_path) == []
    assert not list(tmp_path.glob("*.raw"))  # the scratch row is gone after each row


def test_a_killed_run_resumes_from_its_last_row(use_binary, tmp_path):
    whole = rows_of(run_width(tmp_path / "whole", SLOW_WIDTH, stop_after_row=12))
    first = run_width(tmp_path / "parts", SLOW_WIDTH, stop_after_row=8)
    second = run_width(tmp_path / "parts", SLOW_WIDTH, stop_after_row=12)
    assert second[0] == (MSG_STAGE_START, 0)
    resumed_at = min(rows_of(second))
    assert (
        8 <= resumed_at <= max(rows_of(first)) + 1
    )  # nothing before the checkpoint is redone
    assert {**rows_of(first), **rows_of(second)} == {
        r: n for r, n in whole.items() if r <= max(rows_of(second))
    }
    ExactFiles(tmp_path / "parts", "sib", SLOW_WIDTH).remove()
    assert checkpointed_widths(tmp_path / "parts") == []


def test_bad_arguments_fail_loudly(use_binary, tmp_path):
    for args in (["--width", "0"], ["--cells", "0,0 0,0"], ["--bogus", "1"]):
        done = subprocess.run(
            [
                str(use_binary),
                "--width",
                "10",
                "--cells",
                "0,0 1,0",
                "--checkpoint",
                str(tmp_path / "c"),
                "--scratch",
                str(tmp_path / "s"),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 2 and "error:" in done.stderr
    (tmp_path / "c").write_bytes(b"not a checkpoint")
    done = subprocess.run(
        [
            str(use_binary),
            "--width",
            "10",
            "--cells",
            "0,0 1,0",
            "--checkpoint",
            str(tmp_path / "c"),
            "--scratch",
            str(tmp_path / "s"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2 and "not a checkpoint" in done.stderr


def test_outcome_lines_are_parsed_and_checked():
    lines = [
        "start 4",
        "row 4 states 9 words 3 seconds 5",
        "row 5 states 1 words 0 seconds 7",
        "counts 9 1",
        "dead 5",
    ]
    assert parse_outcome(lines) == ExactOutcome(EXACT_DEAD, 5, 0, 4, (9, 1), 7.0)
    assert parse_outcome(lines[:-1]) is None
    with pytest.raises(ValueError):
        ExactOutcome(EXACT_DEAD, 5, 0, 4, (9,), 7.0)  # one count per row
    with pytest.raises(ValueError):
        ExactOutcome(
            EXACT_FOREVER, 5, 5, 4, (9, 1), 7.0
        )  # it must repeat an earlier row


# the server's side


@pytest.fixture
def exact_config(sib_config):
    return replace(sib_config, exact_ram_mb=2048)


def ask(client, width=None, version=EXACT_SINCE, name=NAME, ram_mb=8192):
    return client.post(
        "/api/jobs/request",
        json={
            "search": SEARCH_EXACT,
            "name": name,
            "max_ram_mb": ram_mb,
            "max_threads": 4,
            "client_version": version,
            "width": width,
        },
    )


def exact_submission(
    key, job, outcome: ExactOutcome, verdict=None, name=NAME, signed_exact=True
):
    verdict = verdict or outcome.verdict
    salt = token.new_salt()
    payload = token.signing_payload(
        job["job_id"],
        job["nonce"],
        name,
        job["width"],
        job["height"],
        verdict,
        0,
        3.0,
        salt,
        None,
        outcome.to_dict() if signed_exact else None,
    )
    return {
        "job_id": job["job_id"],
        "nonce": job["nonce"],
        "name": name,
        "verdict": verdict,
        "stage_index": 0,
        "duration_s": 3.0,
        "peak_ram_mb": 100,
        "salt": salt,
        "token": token.sign(key, payload),
        "client_version": EXACT_SINCE,
        "exact": outcome.to_dict(),
    }


DEAD_10 = ExactOutcome(EXACT_DEAD, 2, 0, 0, (1, 5, 1), 4.0)


def test_exact_jobs_go_to_new_apps_smallest_open_width_first(exact_config):
    with TestClient(create_app(exact_config)) as client:
        assert ask(client, version="0.12.0").status_code == 426
        assert ask(client, ram_mb=1024).status_code == 404  # needs exact_ram_mb
        job = ask(client).json()
        assert (job["lane"], job["width"], job["height"], job["threads"]) == (
            "exact",
            10,
            0,
            1,
        )
        assert [s["exact"] for s in job["stages"]] == [True]
        assert ask(client).json()["width"] == 11  # 10 is running
        assert ask(client, width=10).status_code == 409


def test_an_exact_death_rules_the_width_out(exact_config, submit_key):
    with TestClient(create_app(exact_config)) as client:
        job = ask(client).json()
        no_exact = exact_submission(submit_key, job, DEAD_10)
        del no_exact["exact"]
        assert "only with, an exact job" in post_result(client, no_exact, 422)["detail"]
        wrong = exact_submission(submit_key, job, DEAD_10, verdict="filled")
        assert "does not match" in post_result(client, wrong, 422)["detail"]
        unsigned = exact_submission(submit_key, job, DEAD_10, signed_exact=False)
        post_result(client, unsigned, 403)
        post_result(client, exact_submission(submit_key, job, DEAD_10))

        gone = ask(client, width=10)
        assert gone.status_code == 410 and "ruled out" in gone.text
        strips = client.get("/api/strips").json()
        assert [(r["width"], r["outcome"], r["row"]) for r in strips["exact"]] == [
            (10, "dead", 2)
        ]
        assert any(w["width"] == 10 and w["dead_rows"] == 2 for w in strips["widths"])
        page = client.get("/").text
        assert (
            "Exact width decisions" in page
            and "ruled out: no filling of 2 rows" in page
        )


def test_forever_and_rectangle_outcomes(exact_config, submit_key):
    with TestClient(create_app(exact_config)) as client:
        width, earlier, later = SIB_FOREVER
        job = ask(client, width=width).json()
        forever = ExactOutcome(
            EXACT_FOREVER, later, earlier, 4, tuple(range(later - 3)), 9.0
        )
        post_result(client, exact_submission(submit_key, job, forever))
        refused = client.post(
            "/api/jobs/request",
            json={
                "search": "width",
                "name": NAME,
                "max_ram_mb": 8192,
                "max_threads": 4,
                "client_version": EXACT_SINCE,
                "width": 2 * width,
            },
        )
        assert (
            refused.status_code == 409 and "fills forever" in refused.text
        )  # 24 = 12 + 12
        assert ask(client, width=width).status_code == 410

        width, height = SIB_RECTANGLE
        job = ask(client, width=width).json()
        rectangle = ExactOutcome(
            EXACT_RECTANGLE, height, 0, 4, tuple(range(height - 3)), 9.0
        )
        post_result(client, exact_submission(submit_key, job, rectangle))
        assert f"a {width} x {height} rectangle tiles" in client.get("/").text


def beat_and_release(client, job, resumable: bool) -> None:
    beat = client.post(
        f"/api/jobs/{job['job_id']}/heartbeat",
        json={
            "nonce": job["nonce"],
            "state": "running",
            "stage_index": 0,
            "active_s": 5.0,
            "exact_row": 30,
            "exact_states": 1234,
            "exact_resumable": resumable,
        },
    )
    assert beat.status_code == 200
    assert "row 30 reached (1,234 states)" in client.get("/").text
    client.post(
        f"/api/jobs/{job['job_id']}/release",
        json={"nonce": job["nonce"], "reason": "closed"},
    )


def test_a_checkpointed_width_stays_with_its_volunteer(exact_config):
    with TestClient(create_app(exact_config)) as client:
        # without checkpoints the run cannot be resumed, so the width is free again at once
        beat_and_release(client, ask(client).json(), resumable=False)
        job = ask(client, name="someone else").json()
        assert job["width"] == 10
        beat_and_release(client, job, resumable=True)
        assert ask(client).json()["width"] == 11
        taken = ask(client, width=10, name="another one")
        assert taken.status_code == 409 and "can resume it" in taken.text
        assert ask(client, width=10, name="someone else").json()["width"] == 10


# a real run


def test_a_real_run_decides_widths_and_cleans_up(
    exact_config,
    make_live_server,
    controller,  # noqa: F811 - fixture
    use_binary,
    monkeypatch,
):
    monkeypatch.setattr(controller_module, "MIN_FREE_DISK_MB", 1)
    server = make_live_server(exact_config)
    c, recorder = controller
    run_settings = replace(settings(server.url), search=SEARCH_EXACT)
    c.start(run_settings, widths=WidthQueue((SIB_FOREVER[0],)))
    wait_for(lambda: len(server.db.exact_results()) >= 2, TIMEOUT_S)
    c.stop()
    found = {r.width: r.outcome for r in server.db.exact_results()}
    assert found == {SIB_FOREVER[0]: EXACT_FOREVER, SIB_DEAD[0]: EXACT_DEAD}
    assert recorder.has("FILLS FOREVER") and recorder.has("RULED OUT exactly")
    assert checkpointed_widths(run_settings.exact_dir(c._directory)) == []


def test_the_app_resumes_its_checkpoint_first_and_drops_decided_ones(
    exact_config,
    make_live_server,
    controller,  # noqa: F811 - fixture
    use_binary,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(controller_module, "MIN_FREE_DISK_MB", 1)
    folder = tmp_path / "exact-folder"
    run_width(folder, SLOW_WIDTH, stop_after_row=8)
    (folder / "sib-10.ckpt").write_bytes(b"stale")  # width 10 gets decided below
    server = make_live_server(exact_config)
    with TestClient(server.app) as client:
        job = ask(client, width=SIB_DEAD[0], name="first").json()
        dead = exact_submission(exact_config.submit_key, job, DEAD_10, name="first")
        post_result(client, dead)
    c, recorder = controller
    c.start(
        replace(
            settings(server.url),
            search=SEARCH_EXACT,
            exact_folder=str(folder),
            save_checkpoints=True,
        ).validated()
    )
    wait_for(lambda: recorder.has("resuming from its checkpoint"), TIMEOUT_S)
    status = wait_for(
        lambda: (s := recorder.last_status()) and s.exact_row and s, TIMEOUT_S
    )
    c.stop()
    assert (
        recorder.has("Width 10 was decided meanwhile")
        and not (folder / "sib-10.ckpt").exists()
    )
    assert status.job.width == SLOW_WIDTH and status.exact_row[0] >= 8
    assert os.path.exists(folder / f"sib-{SLOW_WIDTH}.ckpt")
