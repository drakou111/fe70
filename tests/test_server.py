import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tests.test_shared import solve_sibling
from tilingvol.server.app import create_app
from tilingvol.server.scheduler import RAM_FILL
from tilingvol.shared import token
from tilingvol.shared.pieces import SIBLING
from tilingvol.shared.verify import verify_tiling

NAME = "tester"


@pytest.fixture
def client(sib_config):
    with TestClient(create_app(sib_config)) as c:
        yield c


def request_job(client, name=NAME, ram=8192, threads=4, size=None, expect=200):
    body = {
        "name": name,
        "max_ram_mb": ram,
        "max_threads": threads,
        "client_version": "0.1.0",
        "width": size[0] if size else None,
        "height": size[1] if size else None,
    }
    response = client.post("/api/jobs/request", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def submission(
    key, job, verdict, stage_index, duration, solution=None, salt=None, name=NAME
):
    salt = salt or token.new_salt()
    payload = token.signing_payload(
        job["job_id"],
        job["nonce"],
        name,
        job["width"],
        job["height"],
        verdict,
        stage_index,
        duration,
        salt,
        solution,
    )
    return {
        "job_id": job["job_id"],
        "nonce": job["nonce"],
        "name": name,
        "verdict": verdict,
        "stage_index": stage_index,
        "duration_s": duration,
        "peak_ram_mb": 100,
        "salt": salt,
        "token": token.sign(key, payload),
        "client_version": "0.1.0",
        "solution": solution,
    }


def post_result(client, body, expect=200):
    response = client.post(f"/api/jobs/{body['job_id']}/result", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def test_jobs_come_in_area_order_without_duplicates(client):
    first = request_job(client)
    second = request_job(client)
    assert (first["width"], first["height"]) == (10, 14)
    assert (second["width"], second["height"]) != (10, 14)
    assert first["piece"] == "sib" and first["stages"][-1]["border"] == 0


def test_area_lane_follows_area_not_width(client):
    areas = [
        job["width"] * job["height"] for job in (request_job(client) for _ in range(6))
    ]
    assert areas == sorted(areas)
    assert 12 * 14 in areas  # 12 x 14 (168) comes before 10 x 21 (210)


def test_impossible_result_is_recorded_once(client, submit_key):
    job = request_job(client)
    body = submission(submit_key, job, "impossible", 0, 0.5)
    post_result(client, body)
    post_result(client, body, expect=409)  # same job, same salt
    again = request_job(client)
    assert (again["width"], again["height"]) != (job["width"], job["height"])
    rows = client.get("/api/results").json()
    assert [(r["width"], r["height"], r["verdict"]) for r in rows] == [
        (10, 14, "impossible")
    ]


def test_salt_can_never_be_reused(client, submit_key):
    a, b = request_job(client), request_job(client)
    salt = token.new_salt()
    post_result(client, submission(submit_key, a, "impossible", 0, 0.5, salt=salt))
    post_result(
        client, submission(submit_key, b, "impossible", 0, 0.5, salt=salt), expect=409
    )


def test_bad_token_and_tampering_are_rejected(client, submit_key):
    job = request_job(client)
    post_result(
        client, submission(b"wrong key" * 4, job, "impossible", 0, 0.5), expect=403
    )
    body = submission(submit_key, job, "impossible", 0, 0.5)
    body["duration_s"] = 0.6
    post_result(client, body, expect=403)
    body = submission(submit_key, job, "impossible", 0, 0.5)
    body["nonce"] = "0" * 32
    post_result(client, body, expect=404)


def test_impossible_durations_are_checked(client, submit_key):
    job = request_job(client)
    post_result(
        client, submission(submit_key, job, "timed_out", 0, 5.0), expect=422
    )  # below the 60 s limit
    post_result(
        client, submission(submit_key, job, "impossible", 0, 3600.0), expect=422
    )  # job is newer


def test_possible_needs_a_valid_tiling(client, submit_key):
    job = request_job(client, size=(19, 28))
    tiling = solve_sibling(19, 28)
    broken = [list(p) for p in tiling]
    broken[0][1] += 1
    post_result(
        client,
        submission(submit_key, job, "possible", 1, 1.0, solution=broken),
        expect=422,
    )
    post_result(
        client,
        submission(submit_key, job, "possible", 0, 1.0, solution=tiling),
        expect=422,
    )  # frame stage
    post_result(
        client, submission(submit_key, job, "possible", 1, 1.0, solution=tiling)
    )
    stored = client.get("/api/solution/19x28").json()
    verify_tiling(SIBLING, 19, 28, stored["placements"])
    assert "TILING" in client.get("/").text.upper()


def test_requested_sizes(client):
    job = request_job(client, size=(21, 30))
    assert job["lane"] == "requested"
    request_job(client, size=(21, 30), expect=409)  # running
    request_job(client, size=(11, 13), expect=409)  # 7 does not divide 143


def test_give_up_returns_size_to_pool(client):
    job = request_job(client)
    response = client.post(
        f"/api/jobs/{job['job_id']}/release",
        json={"nonce": job["nonce"], "reason": "gave_up"},
    )
    assert response.status_code == 200
    again = request_job(client)
    assert (again["width"], again["height"]) == (job["width"], job["height"])


def test_lease_expires_without_heartbeat(client, sib_config):
    job = request_job(client)
    db = client.app.state.db
    with db._lock:
        db._conn.execute(
            "UPDATE jobs SET heartbeat = ? WHERE job_id = ?",
            (time.time() - sib_config.lease_timeout_s - 1, job["job_id"]),
        )
    again = request_job(client)
    assert (again["width"], again["height"]) == (job["width"], job["height"])
    beat = client.post(
        f"/api/jobs/{job['job_id']}/heartbeat",
        json={
            "nonce": job["nonce"],
            "state": "running",
            "stage_index": 0,
            "active_s": 1,
        },
    )
    assert beat.status_code == 410


def test_ram_limits_threads_and_sizes(client):
    job = request_job(client, ram=500, threads=16)
    assert job["est_ram_mb"] <= RAM_FILL * 500
    assert 1 <= job["threads"] < 16
    request_job(client, ram=256, threads=1, expect=404)


def test_oversized_and_malformed_requests(client):
    assert (
        client.post(
            "/api/jobs/request",
            content=b"x" * (5 << 20),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/api/jobs/request",
            json={
                "name": "",
                "max_ram_mb": 1000,
                "max_threads": 1,
                "client_version": "0.1.0",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/jobs/request",
            json={
                "name": "x" * 41,
                "max_ram_mb": 1000,
                "max_threads": 1,
                "client_version": "0.1.0",
            },
        ).status_code
        == 422
    )
    request_job(client, name="old", expect=200)
    body = {
        "name": "old",
        "max_ram_mb": 1000,
        "max_threads": 1,
        "client_version": "0.0.1",
    }
    assert client.post("/api/jobs/request", json=body).status_code == 426


def test_page_escapes_names(client, submit_key):
    evil = "<script>alert(1)</script>"
    job = request_job(client, name=evil)
    post_result(client, submission(submit_key, job, "impossible", 0, 0.5, name=evil))
    html = client.get("/").text
    assert "<script>alert" not in html and "&lt;script&gt;" in html


def add_import(app, width, height, stage_tag, duration=100.0):
    app.state.db.add_result(
        job_id=None,
        width=width,
        height=height,
        verdict="impossible",
        stage_tag=stage_tag,
        duration_s=duration,
        name="seed",
        ip=None,
        threads=None,
        peak_ram_mb=None,
        est_ram_mb=None,
        client_version=None,
        flagged=False,
        flag_reason="",
        hard=False,
        source="import",
        solution=None,
    )


def test_adaptive_borders_follow_the_nearest_settled_size(sib_config):
    config = replace(
        sib_config,
        adaptive_borders=True,
        default_border=3,
        border_step=2,
        border_increments=2,
    )
    with TestClient(create_app(config)) as c:
        job = request_job(c, size=(20, 21))
        assert [s["border"] for s in job["stages"]] == [
            3,
            5,
            7,
            0,
        ]  # nothing settled yet
        add_import(c.app, 14, 15, "frame4L")
        add_import(c.app, 28, 30, "frame6L")
        job = request_job(c, size=(14, 21))
        assert [s["border"] for s in job["stages"]] == [
            4,
            6,
            7,
            0,
        ]  # nearest is 14 x 15; capped at 14 // 2
        job = request_job(c, size=(28, 28))
        assert [s["border"] for s in job["stages"]] == [6, 8, 10, 0]
        assert job["stages"][-1]["lookahead"] is False


def test_access_tokens(sib_config):
    token = "t" * 20
    with TestClient(
        create_app(replace(sib_config, access_tokens=frozenset({token})))
    ) as c:
        body = {
            "name": "x",
            "max_ram_mb": 4096,
            "max_threads": 2,
            "client_version": "0.3.0",
        }
        assert c.post("/api/jobs/request", json=body).status_code == 401
        assert (
            c.post(
                "/api/jobs/request", json=body, headers={"X-Access-Token": "wrong" * 4}
            ).status_code
            == 401
        )
        job = c.post("/api/jobs/request", json=body, headers={"X-Access-Token": token})
        assert job.status_code == 200
        beat = {
            "nonce": job.json()["nonce"],
            "state": "running",
            "stage_index": 0,
            "active_s": 1,
        }
        assert (
            c.post(f"/api/jobs/{job.json()['job_id']}/heartbeat", json=beat).status_code
            == 401
        )
        assert c.get("/").status_code == 200  # the public page stays public


def test_border_pictures_are_checked_before_display(client, submit_key):
    tiling = solve_sibling(19, 28)
    border = [
        p for p in tiling if any(min(x, y, 18 - x, 27 - y) < 3 for x, y in cells_of(p))
    ]
    job = request_job(client, size=(19, 28))
    body = submission(submit_key, job, "impossible", 1, 1.0)
    post_result(client, {**body, "frame_border": 3, "frame_placements": border})
    rows = client.app.state.db.recent_pictures(5)
    assert [(r.width, r.height, r.border) for r in rows] == [(19, 28, 3)]
    assert client.get(f"/picture/{rows[0].result_id}.png").content.startswith(
        b"\x89PNG"
    )
    job = request_job(client)
    body = submission(submit_key, job, "impossible", 1, 1.0)
    bogus = [
        [0, 0, 0]
    ]  # does not fill the border: the result counts, the picture is dropped
    post_result(client, {**body, "frame_border": 3, "frame_placements": bogus})
    assert len(client.app.state.db.recent_pictures(5)) == 1


def cells_of(placement):
    orientation, x0, y0 = placement
    return [(x0 + dx, y0 + dy) for dx, dy in SIBLING.orientations[orientation]]


def test_no_time_limit_jobs(client):
    body = {
        "name": NAME,
        "max_ram_mb": 8192,
        "max_threads": 4,
        "client_version": "0.6.0",
        "no_time_limit": True,
    }
    job = client.post("/api/jobs/request", json=body).json()
    assert all(s["time_limit_s"] == 7 * 24 * 3600 for s in job["stages"])
    assert all(s["time_limit_s"] < 7 * 24 * 3600 for s in request_job(client)["stages"])
