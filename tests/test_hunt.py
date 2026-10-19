"""The symmetric hunt and custom stages: model, server rules, import, and one real run."""

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ortools.sat.python import cp_model

from tests.conftest import wait_for
from tests.test_end_to_end import Recorder, settings
from tests.test_server import add_import, cells_of, post_result, submission
from tilingvol.client.controller import Controller
from tilingvol.client.model import build_model
from tilingvol.server.app import create_app
from tilingvol.server.import_sweep import parse
from tilingvol.shared.pieces import SIBLING
from tilingvol.shared.protocol import parse_custom_stages
from tilingvol.shared.verify import TilingError, verify_symmetric, verify_tiling

NAME = "hunter"
SYMMETRIC_SIZE = (18, 35)  # the sibling's smallest rectangle with a symmetric tiling
ASYMMETRIC_ONLY = (19, 28)  # tiles, but never symmetrically


def solve(width, height, border=0, lookahead=False, symmetric=True):
    built = build_model(SIBLING, width, height, border, lookahead, symmetric)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 4
    solver.parameters.max_time_in_seconds = 60
    status = solver.Solve(built.model)
    placements = [
        list(built.placement_of[c])
        for c, v in built.variables.items()
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) and solver.Value(v)
    ]
    return solver.StatusName(status), placements


def test_symmetric_model_finds_only_symmetric_tilings():
    status, placements = solve(*SYMMETRIC_SIZE)
    assert status == "OPTIMAL"
    verify_tiling(SIBLING, *SYMMETRIC_SIZE, placements)
    verify_symmetric(SIBLING, *SYMMETRIC_SIZE, placements)
    # a size that tiles, but has no symmetric tiling: the hunt's "impossible" proves nothing
    assert solve(*ASYMMETRIC_ONLY)[0] == "INFEASIBLE"
    assert solve(*ASYMMETRIC_ONLY, symmetric=False)[0] == "OPTIMAL"
    status, border = solve(*SYMMETRIC_SIZE, border=3, lookahead=True)
    assert status == "OPTIMAL"
    verify_symmetric(SIBLING, *SYMMETRIC_SIZE, border)


def test_verify_symmetric_rejects_a_lone_piece():
    with pytest.raises(TilingError):
        verify_symmetric(SIBLING, 18, 35, [[0, 0, 0]])


def test_custom_stage_text():
    assert parse_custom_stages("") == ()
    assert parse_custom_stages(" 24, 28 32 ") == (24, 28, 32)
    for bad in ("28, 24", "24, 24", "0", "x", "1,2,3,4,5,6,7,8,9"):
        with pytest.raises(ValueError):
            parse_custom_stages(bad)


def ask(client, hunt=False, custom=(), size=None, expect=200):
    body = {
        "name": NAME,
        "max_ram_mb": 8192,
        "max_threads": 4,
        "client_version": "0.9.0",
        "hunt": hunt,
        "custom_stages": list(custom),
        "width": size[0] if size else None,
        "height": size[1] if size else None,
    }
    response = client.post("/api/jobs/request", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def release(client, job):
    body = {"nonce": job["nonce"], "reason": "gave_up"}
    assert (
        client.post(f"/api/jobs/{job['job_id']}/release", json=body).status_code == 200
    )


def test_hunt_keeps_its_own_books(sib_config, submit_key):
    with TestClient(create_app(sib_config)) as c:
        job = ask(c, hunt=True, size=ASYMMETRIC_ONLY)
        assert job["lane"] == "hunt"
        assert all(s["symmetric"] for s in job["stages"])
        body = submission(
            submit_key, job, "impossible", len(job["stages"]) - 1, 1.0, name=NAME
        )
        post_result(c, body)
        # no symmetric tiling: the hunt is done with the size, the main search is not
        ask(c, hunt=True, size=ASYMMETRIC_ONLY, expect=409)
        assert c.app.state.db.results("main") == []
        page = c.get("/").text
        assert "Symmetric hunt" in page and "Sizes with no symmetric tiling" in page
        # on the map in its own colour, never as ruled out
        assert (
            "19 x 28: no symmetric tiling (an asymmetric one is still possible)" in page
        )
        assert "19 x 28: impossible" not in page
        main = ask(c, size=ASYMMETRIC_ONLY)
        assert not any(s["symmetric"] for s in main["stages"])
        release(c, main)
        # a size the main search settles is closed for the hunt too
        add_import(c.app, 18, 35, "full")
        ask(c, hunt=True, size=SYMMETRIC_SIZE, expect=409)
        # main and hunt can run the same size at once
        hunt_job = ask(c, hunt=True, size=(14, 20))
        main_job = ask(c, size=(14, 20))
        assert hunt_job["lane"] == "hunt" and main_job["lane"] == "requested"


def test_symmetric_frames_are_checked_and_only_they_skip_hunt_stages(sib_config):
    config = replace(
        sib_config,
        adaptive_borders=True,
        default_border=3,
        border_step=2,
        border_increments=2,
    )
    _, border = solve(*SYMMETRIC_SIZE, border=3, lookahead=True)
    _, asymmetric = solve(*ASYMMETRIC_ONLY, symmetric=False)
    width, height = ASYMMETRIC_ONLY
    lopsided = [  # a genuine depth-3 border filling, but not a symmetric one
        p
        for p in asymmetric
        if any(min(x, y, width - 1 - x, height - 1 - y) < 3 for x, y in cells_of(p))
    ]
    with TestClient(create_app(config)) as c:
        job = ask(c, hunt=True, size=ASYMMETRIC_ONLY)
        frame = {"nonce": job["nonce"], "stage_index": 0, "placements": lopsided}
        response = c.post(f"/api/jobs/{job['job_id']}/frame", json=frame)
        assert response.status_code == 422 and "twin" in response.text
        release(c, job)
        job = ask(c, hunt=True, size=SYMMETRIC_SIZE)
        assert [s["border"] for s in job["stages"]] == [3, 5, 7, 0]
        frame = {"nonce": job["nonce"], "stage_index": 0, "placements": border}
        assert c.post(f"/api/jobs/{job['job_id']}/frame", json=frame).status_code == 200
        release(c, job)
        again = ask(c, hunt=True, size=SYMMETRIC_SIZE)
        assert [s["border"] for s in again["stages"]] == [5, 7, 0]
        release(c, again)
        # a symmetric filling is an ordinary one too
        main = ask(c, size=SYMMETRIC_SIZE)
        assert [s["border"] for s in main["stages"]] == [5, 7, 0]
        assert c.app.state.db.recent_pictures(5)[0].symmetric


def test_custom_stages(sib_config):
    with TestClient(create_app(sib_config)) as c:
        job = ask(c, custom=(2, 4, 30), size=(14, 20))
        # depths past half the short side are capped, and the full search always comes last
        assert [s["border"] for s in job["stages"]] == [2, 4, 7, 0]
        release(c, job)
        hunt = ask(c, hunt=True, custom=(3,), size=(14, 20))
        assert [(s["border"], s["symmetric"]) for s in hunt["stages"]] == [
            (3, True),
            (0, True),
        ]
        ask(c, custom=(4, 2), expect=422)
        ask(c, custom=(0,), expect=422)


def test_symmetric_sweep_lines_import_as_hunt_results(tmp_path):
    sweep = tmp_path / "sym_hunt3.txt"
    sweep.write_text(
        "# run 2026-10-07 sym=180\n"
        "gun 136x135 sym=180: UNSAT (118s, 139340 placements)\n"
        "gun 144x195 sym=180: UNKNOWN(timeout) (1202s, 215196 placements)\n",
        encoding="utf-8",
    )
    rows = parse(Path(sweep))
    assert [(r["width"], r["height"], r["verdict"], r["stage_tag"]) for r in rows] == [
        (135, 136, "impossible", "symfull"),
        (144, 195, "timed_out", "symfull"),
    ]


def test_hunt_finds_a_symmetric_tiling_end_to_end(live_server, tmp_path, submit_key):
    recorder = Recorder()
    c = Controller(tmp_path, submit_key, recorder)
    c.launch()
    try:
        c.start(
            replace(settings(live_server.url), symmetric_hunt=True),
            requested=SYMMETRIC_SIZE,
        )
        wait_for(lambda: live_server.db.solution(*SYMMETRIC_SIZE) is not None, 120)
    finally:
        c.shutdown()
    solution = live_server.db.solution(*SYMMETRIC_SIZE)
    verify_symmetric(SIBLING, *SYMMETRIC_SIZE, solution)
    # a tiling from the hunt settles the size for everyone
    assert any(r.verdict == "possible" for r in live_server.db.results("main"))
    assert any(e.hunt for e in recorder.events if e.kind == "picture")


def test_hunt_pictures_have_their_own_colours():
    from tilingvol.shared.picture import HUNT_PALETTE, PALETTE, render_png, render_rgb

    _, placements = solve(*SYMMETRIC_SIZE)
    assert not set(HUNT_PALETTE) & set(PALETTE)
    normal = render_png(SIBLING, *SYMMETRIC_SIZE, placements)
    assert render_png(SIBLING, *SYMMETRIC_SIZE, placements, symmetric=True) != normal
    _, _, pixels = render_rgb(SIBLING, *SYMMETRIC_SIZE, placements, HUNT_PALETTE)
    assert bytes(HUNT_PALETTE[placements[0][0]]) in pixels
