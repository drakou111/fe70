"""Half-strip depth jobs: the model, the checker, the scheduler's strip lane, the page, and a real run."""

import secrets
import urllib.request
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from ortools.sat.python import cp_model

from tests.conftest import wait_for
from tests.test_end_to_end import controller, settings  # noqa: F401 - fixture
from tests.test_server import NAME, PNG_MAGIC, post_result, submission
from tilingvol.client.api import ServerError, validate_job
from tilingvol.client.model import build_strip_model
from tilingvol.server import known_strips
from tilingvol.server.app import create_app
from tilingvol.server.db import Database
from tilingvol.server.scheduler import NoAssignment, Scheduler
from tilingvol.shared.pieces import GUN, SIBLING
from tilingvol.shared.protocol import STRIP_SINCE, Job, Stage
from tilingvol.shared.verify import TilingError, verify_strip


def solve_strip(piece, width: int, rows: int) -> list[list[int]] | None:
    built = build_strip_model(piece, width, rows)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 4
    status = solver.Solve(built.model)
    if status == cp_model.INFEASIBLE:
        return None
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    return [
        list(built.placement_of[cells])
        for cells, v in built.variables.items()
        if solver.Value(v)
    ]


@pytest.mark.parametrize("width, death_row", [(17, 25), (25, 12), (26, 15), (27, 13)])
def test_strip_model_matches_the_exact_death_rows(width, death_row):
    # death rows from the research repo's exact hsw computation (output/halfstrip_widths.txt)
    filling = solve_strip(GUN, width, death_row - 1)
    assert filling is not None
    verify_strip(GUN, width, death_row - 1, filling)
    assert solve_strip(GUN, width, death_row) is None


def test_a_width_too_narrow_for_any_piece_dies_at_once():
    assert solve_strip(GUN, 1, 3) is None


def test_strip_checker_rejects_bad_fillings():
    filling = solve_strip(SIBLING, 12, 6)
    verify_strip(SIBLING, 12, 6, filling)
    with pytest.raises(TilingError, match="not covered"):
        verify_strip(SIBLING, 12, 6, filling[1:])
    with pytest.raises(TilingError, match="twice"):
        verify_strip(SIBLING, 12, 6, filling + [filling[0]])
    with pytest.raises(TilingError, match="outside"):
        verify_strip(SIBLING, 11, 6, filling)
    with pytest.raises(TilingError, match="does not start"):
        verify_strip(SIBLING, 12, 6, filling + [[0, 0, 6]])


def test_the_client_rejects_malformed_strip_jobs():
    def job(depths, height=None):
        stages = tuple(Stage(0, False, 60.0, strip_rows=d) for d in depths)
        return Job(
            "a" * 32,
            "b" * 32,
            "sib",
            12,
            height or depths[-1],
            stages,
            1,
            100,
            "strip",
            60.0,
        )

    assert validate_job(job((8, 16)), 4).strip
    for bad in (job((16, 8)), job((8, 8)), job((8, 16), height=20), job((0, 8))):
        with pytest.raises(ServerError, match="strip"):
            validate_job(bad, 4)


@pytest.fixture
def strip_config(sib_config):
    return replace(
        sib_config,
        strip_first_rows=4,
        strip_step=4,
        strip_stages=2,
    )


@pytest.fixture
def strip_client(strip_config):
    with TestClient(create_app(strip_config)) as c:
        yield c


def request(
    client, version=STRIP_SINCE, size=None, no_time_limit=False, search="width"
):
    body = {
        "search": search,
        "name": NAME,
        "max_ram_mb": 8192,
        "max_threads": 4,
        "client_version": version,
        "width": size[0] if size else None,
        "height": size[1] if size else None,
        "no_time_limit": no_time_limit,
    }
    response = client.post("/api/jobs/request", json=body)
    return response


def test_strip_jobs_go_to_new_apps_only_cheapest_width_first(strip_client):
    first = request(strip_client).json()
    second = request(strip_client).json()
    assert first["lane"] == second["lane"] == "strip"
    assert (first["width"], second["width"]) == (10, 11)
    assert [s["strip_rows"] for s in first["stages"]] == [4, 8]
    assert first["height"] == 8
    board = request(strip_client, search="board").json()
    assert board["lane"] != "strip" and not any(
        s["strip_rows"] for s in board["stages"]
    )
    old = request(strip_client, version="0.9.0", search=None).json()
    assert old["lane"] != "strip" and not any(
        s.get("strip_rows") for s in old["stages"]
    )
    too_old = request(strip_client, version="0.9.0")
    assert too_old.status_code == 426
    assert request(strip_client, search="nonsense").status_code == 422


def test_a_volunteer_can_ask_for_one_width(strip_client, submit_key):
    job = request(strip_client, size=(17, None)).json()
    assert (job["lane"], job["width"]) == ("strip", 17)
    taken = request(strip_client, size=(17, None))
    assert taken.status_code == 409 and "being run" in taken.text
    post_result(strip_client, submission(submit_key, job, "impossible", 0, 3.0))
    dead = request(strip_client, size=(17, None))
    assert dead.status_code == 409 and "ruled out" in dead.text
    assert request(strip_client, size=(9, None)).status_code == 409


def test_a_ruled_out_width_leaves_the_map(strip_client, submit_key):
    job = request(strip_client).json()
    assert job["width"] == 10
    body = submission(submit_key, job, "impossible", 0, 3.0)
    post_result(strip_client, body)
    states = strip_client.get("/api/strips").json()["widths"]
    assert {"width": 10, "dead_rows": 4, "dead_by": NAME}.items() <= states[0].items()
    refused = request(strip_client, size=(10, 14), search="board")
    assert refused.status_code == 409 and "excluded" in refused.text
    rect = request(strip_client, search="board").json()
    assert 10 not in (rect["width"], rect["height"])
    page = strip_client.get("/").text
    assert "Widths ruled out" in page and "RULED OUT, the first 4 rows" in page
    assert "or equal to 10" in page


def test_strip_fillings_are_checked_shown_and_continued_deeper(
    strip_client, submit_key
):
    job = request(strip_client).json()
    while job["width"] != 12:  # 10 and 11 die at once for the sibling; 12 can be filled
        job = request(strip_client).json()
    filling = solve_strip(SIBLING, 12, 4)
    bad = strip_client.post(
        f"/api/jobs/{job['job_id']}/frame",
        json={"nonce": job["nonce"], "stage_index": 0, "placements": filling[1:]},
    )
    assert bad.status_code == 422 and "strip filling" in bad.text
    good = strip_client.post(
        f"/api/jobs/{job['job_id']}/frame",
        json={"nonce": job["nonce"], "stage_index": 0, "placements": filling},
    )
    assert good.status_code == 200
    assert strip_client.get("/strip/1.png").content.startswith(PNG_MAGIC)
    assert "Latest strip fillings" in strip_client.get("/").text

    for verdict, index in (("possible", 1), ("filled", 0)):
        post_result(strip_client, submission(submit_key, job, verdict, index, 3.0), 422)
    post_result(strip_client, submission(submit_key, job, "filled", 1, 3.0))
    widths = {w["width"]: w for w in strip_client.get("/api/strips").json()["widths"]}
    assert widths[12]["filled_rows"] == 8 and widths[12]["dead_rows"] is None


def strip_scheduler(config, results: list[tuple[int, int, str]]) -> Scheduler:
    db = Database(config.db_path)
    for width, rows, verdict in results:
        db.add_strip_result(
            job_id=None,
            width=width,
            rows=rows,
            verdict=verdict,
            duration_s=1.0,
            name=NAME,
            ip=None,
            threads=None,
            peak_ram_mb=None,
            client_version=None,
            source="volunteer",
        )
    return Scheduler(db, config)


def pick_strip(scheduler: Scheduler, no_time_limit: bool = False):
    """Pick and lease, as the server does."""
    a = scheduler.pick(
        NAME, "1.2.3.4", 8192, 4, None, no_time_limit=no_time_limit, strip=True
    )
    stages = [s.to_dict() for s in a.stages]
    scheduler.db.add_job(
        secrets.token_hex(16),
        secrets.token_hex(16),
        a.width,
        a.height,
        NAME,
        "1.2.3.4",
        a.lane,
        stages,
        a.threads,
        a.est_ram_mb,
        STRIP_SINCE,
    )
    return a


def test_the_cheapest_width_x_next_depth_goes_first(strip_config):
    scheduler = strip_scheduler(
        strip_config, [(10, 20, "filled"), (11, 4, "timed_out"), (13, 4, "filled")]
    )
    assert scheduler.strip_depths(scheduler.width_states()[10]) == (24, 28)
    assert scheduler.strip_depths(scheduler.width_states()[11]) == (8, 12)
    order = [pick_strip(scheduler) for _ in range(4)]
    # 12 x 4 = 48, 11 x 8 = 88, 13 x 8 = 104, 14 x 4 = 56: untried widths win until they cost more
    assert [(a.width, a.stages[0].strip_rows) for a in order] == [
        (12, 4),
        (14, 4),
        (15, 4),
        (16, 4),
    ]
    assert all(a.lane == "strip" for a in order)


def test_widths_that_keep_timing_out_wait_for_unlimited_volunteers(strip_config):
    others = [(w, 4, "impossible") for w in range(11, 41)]
    scheduler = strip_scheduler(
        strip_config, [(10, 4, "timed_out"), (10, 8, "timed_out"), *others]
    )
    with pytest.raises(NoAssignment):  # and every rectangle size has a ruled-out side
        pick_strip(scheduler)
    unlimited = pick_strip(scheduler, no_time_limit=True)
    assert (unlimited.lane, unlimited.width) == ("strip", 10)
    assert [s.strip_rows for s in unlimited.stages] == [12, 16]


def test_known_results_are_seeded_once(tmp_path, sib_config):
    config = replace(sib_config, piece=GUN, db_path=tmp_path / "gun.sqlite3")
    create_app(config)
    create_app(config)
    rows = Database(config.db_path).strip_results()
    assert len(rows) == len(known_strips.GUN_STRIPS)
    dead = {r.width for r in rows if r.verdict == "impossible"}
    assert dead == {135, 141, 142, 143}
    scheduler = Scheduler(Database(config.db_path), config)
    assert scheduler.is_dead((140, 142)) and not scheduler.is_dead((140, 144))


def test_a_real_run_rules_out_widths_and_sends_pictures(
    strip_config,
    make_live_server,
    controller,  # noqa: F811 - fixture
):
    server = make_live_server(strip_config)
    c, recorder = controller
    c.start(replace(settings(server.url), search="width"))
    wait_for(lambda: server.db.strip_fills(), 120)
    c.stop()
    dead = {r.width for r in server.db.strip_results() if r.verdict == "impossible"}
    assert {10, 11} <= dead
    assert recorder.has("RULED OUT")
    pictures = [e for e in recorder.events if e.kind == "picture"]
    assert pictures and all(e.strip for e in pictures)
    fill = server.db.strip_fills()[0]
    png = urllib.request.urlopen(f"{server.url}/strip/{fill.fill_id}.png").read()
    assert png.startswith(PNG_MAGIC)
