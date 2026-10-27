"""Fill-forever searches: repeating half-strip fillings, their checker, the lane and a real run."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from ortools.sat.python import cp_model

from tests.conftest import wait_for
from tests.test_end_to_end import controller, settings  # noqa: F401 - fixture
from tests.test_server import PNG_MAGIC, post_result, submission
from tests.test_strip import request
from tilingvol.client.api import ServerError, validate_job
from tilingvol.client.model import build_periodic_model
from tilingvol.client.width_queue import WidthQueue
from tilingvol.server.app import create_app
from tilingvol.shared.pieces import SIBLING
from tilingvol.shared.protocol import PERIODIC_SINCE, Job, Stage
from tilingvol.shared.verify import TilingError, verify_periodic

# for the sibling piece, width 19 repeats every 14 rows from row 6, but not every 7
WIDTH, START, PERIOD, NO_PERIOD = 19, 6, 14, 7


def solve_periodic(width: int, start: int, period: int) -> list[list[int]] | None:
    built = build_periodic_model(SIBLING, width, start, period)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 4
    if solver.Solve(built.model) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return [
        list(built.placement_of[c])
        for c, v in built.variables.items()
        if solver.Value(v)
    ]


@pytest.fixture(scope="module")
def certificate() -> list[list[int]]:
    found = solve_periodic(WIDTH, START, PERIOD)
    assert found is not None
    return found


def test_the_checker_accepts_a_repeating_filling_and_nothing_else(certificate):
    verify_periodic(SIBLING, WIDTH, START, PERIOD, certificate)
    assert solve_periodic(WIDTH, START, NO_PERIOD) is None
    with pytest.raises(TilingError):
        verify_periodic(SIBLING, WIDTH, START, PERIOD, certificate[1:])
    block = [p for p in certificate if p[2] >= START]
    with pytest.raises(TilingError):  # the block no longer sits on its own top
        verify_periodic(
            SIBLING,
            WIDTH,
            START,
            PERIOD,
            [p for p in certificate if p not in block[:1]],
        )
    with pytest.raises(TilingError):  # the same pieces read with another period
        verify_periodic(SIBLING, WIDTH, START, PERIOD + 1, certificate)
    for start, period in ((START, 6), (SIBLING.strip_reach - 1, PERIOD)):
        with pytest.raises(TilingError, match="out of range"):
            verify_periodic(SIBLING, WIDTH, start, period, certificate)


def periodic_job(stages, height) -> Job:
    return Job("a" * 32, "b" * 32, "sib", 12, height, stages, 2, 1000, "periodic", 60.0)


def test_the_client_rejects_malformed_periodic_jobs():
    good = (Stage(0, False, 60.0, periodic_start=6, period=7),)
    validate_job(periodic_job(good, 13), 4)
    for stages, height in (
        ((Stage(0, False, 60.0, periodic_start=6, period=3),), 9),
        ((Stage(0, False, 60.0, periodic_start=1, period=7),), 8),
        ((Stage(0, False, 60.0, strip_rows=8, periodic_start=6, period=7),), 13),
        (good, 99),
    ):
        with pytest.raises(ServerError, match="periodic"):
            validate_job(periodic_job(stages, height), 4)


@pytest.fixture
def periodic_config(sib_config):
    return replace(
        sib_config,
        periodic_configs=((START, NO_PERIOD), (START, PERIOD), (START, 21)),
        periodic_stages=2,
    )


def forever(client, width=None, version=PERIODIC_SINCE):
    return request(client, version=version, size=(width, None), search="forever")


def test_a_found_repeating_filling_settles_the_width(
    periodic_config, submit_key, certificate
):
    with TestClient(create_app(periodic_config)) as client:
        assert forever(client, version="0.11.2").status_code == 426
        job = forever(client, WIDTH).json()
        assert job["lane"] == "periodic" and job["width"] == WIDTH
        assert [(s["periodic_start"], s["period"]) for s in job["stages"]] == [
            (START, NO_PERIOD),
            (START, PERIOD),
        ]
        assert job["height"] == START + max(PERIOD, NO_PERIOD)
        bad = submission(submit_key, job, "possible", 1, 3.0, certificate[1:])
        assert "not valid" in post_result(client, bad, 422)["detail"]
        post_result(
            client, submission(submit_key, job, "possible", 1, 3.0, certificate)
        )

        found = client.get("/api/strips").json()["forever"]
        assert [(f["width"], f["start"], f["period"]) for f in found] == [
            (WIDTH, START, PERIOD)
        ]
        assert client.get(found[0]["picture"]).content.startswith(PNG_MAGIC)
        page = client.get("/").text
        assert "Widths that fill forever" in page and "repeating every 14 rows" in page
        for width in (WIDTH, 2 * WIDTH):  # 24 is two width-12 fillings side by side
            refused = request(client, size=(width, None))
            assert refused.status_code == 409 and "fills forever" in refused.text
        again = forever(client, WIDTH)
        assert (
            again.status_code == 409 and "already known to fill forever" in again.text
        )


def test_settings_without_a_repeating_filling_are_not_tried_again(
    periodic_config, submit_key
):
    with TestClient(create_app(periodic_config)) as client:
        job = forever(client, 13).json()
        post_result(client, submission(submit_key, job, "impossible", 1, 3.0))
        job = forever(client, 13).json()
        assert [(s["periodic_start"], s["period"]) for s in job["stages"]] == [
            (START, 21)
        ]
        post_result(client, submission(submit_key, job, "impossible", 0, 3.0))
        done = forever(client, 13)
        assert done.status_code == 409 and "every repeat setting" in done.text
        assert forever(client).json()["width"] != 13


def test_a_real_run_proves_a_width_fills_forever(
    periodic_config,
    make_live_server,
    controller,  # noqa: F811 - fixture
):
    server = make_live_server(periodic_config)
    c, recorder = controller
    queue = WidthQueue((WIDTH,))
    c.start(replace(settings(server.url), search="forever"), widths=queue)
    wait_for(
        lambda: any(r.verdict == "found" for r in server.db.periodic_results()), 120
    )
    c.stop()
    rows = server.db.periodic_results()
    assert [(r.width, r.period, r.verdict) for r in rows[:2]] == [
        (WIDTH, NO_PERIOD, "none"),
        (WIDTH, PERIOD, "found"),
    ]
    assert recorder.has("FILLS FOREVER")
    pictures = [e for e in recorder.events if e.kind == "picture"]
    assert pictures and pictures[0].picture[3] == 0 and pictures[0].strip


def test_cylinder_theorems_rule_out_hopeless_periods():
    from tilingvol.shared.pieces import GUN

    assert not GUN.periodic_ruled_out(144, 10) and not GUN.periodic_ruled_out(144, 40)
    for period in (10, 20, 40, 50):  # odd widths: no walled cylinder up to 54 at all
        assert GUN.periodic_ruled_out(137, period)
    assert GUN.periodic_ruled_out(144, 15) and GUN.periodic_ruled_out(137, 70)
    assert not GUN.periodic_ruled_out(137, 60) and not GUN.periodic_ruled_out(137, 80)


def test_odd_widths_get_only_possible_periods(sib_config):
    from tilingvol.server.db import Database
    from tilingvol.server.scheduler import Scheduler
    from tilingvol.shared.pieces import GUN

    config = replace(
        sib_config, piece=GUN, periodic_configs=((60, 10), (110, 20), (60, 60))
    )
    scheduler = Scheduler(Database(config.db_path), config)
    assert scheduler.periodic_configs_left(137, [], False) == [(60, 60)]
    assert scheduler.periodic_configs_left(144, [], False) == [
        (60, 10),
        (110, 20),
        (60, 60),
    ]
