"""Coordination API. Run with: uvicorn --factory tilingvol.server.app:create_app --host 127.0.0.1 --port 8000

Security notes: listen on 127.0.0.1 only (cloudflared forwards to it); every body is size-capped and
validated by pydantic; nothing from a request is executed, unpickled or passed to a shell; SQL is
parameterized; names are HTML-escaped on the page."""

import hmac
import itertools
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from tilingvol.server import known_strips, page
from tilingvol.server.config import ServerConfig
from tilingvol.server.db import (
    PERIODIC_FOUND,
    PERIODIC_NONE,
    PERIODIC_TIMED_OUT,
    Database,
)
from tilingvol.server.scheduler import NoAssignment, RequestRefused, Scheduler
from tilingvol.shared import token
from tilingvol.shared.picture import render_png
from tilingvol.shared.protocol import (
    LANE_HUNT,
    LANE_PERIODIC,
    LANE_STRIP,
    MAX_BORDER,
    MAX_CUSTOM_STAGES,
    MAX_PEAK_RAM_MB,
    NO_LIMIT,
    PERIODIC_SINCE,
    RELEASE_REASONS,
    SEARCH_FOREVER,
    SEARCH_SYMMETRIC,
    SEARCH_WIDTH,
    SEARCHES,
    STRIP_SINCE,
    SYMMETRIC_TAG_PREFIX,
    VERDICT_FILLED,
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
    VERDICTS,
    Stage,
)
from tilingvol.shared.verify import (
    TilingError,
    verify_border,
    verify_periodic,
    verify_strip,
    verify_symmetric,
    verify_tiling,
)

log = logging.getLogger("tilingvol.server")

MAX_BODY_BYTES = 4 << 20
PICTURE_CACHE_SIZE = 64
LEGACY_UNLIMITED_STAGE_S = 7 * 24 * 3600.0  # the longest stage apps before 0.7.0 accept
NO_LIMIT_SINCE = "0.7.0"
TOKEN_HEADER = "x-access-token"
DURATION_SLACK_S = 120.0
IMPOSSIBLE_SLACK_S = 600.0
TIMEOUT_MIN_FRACTION = 0.95
PAGE_CACHE_S = 30.0
HEX = re.compile(r"[0-9a-f]{32}")
TOKEN_HEX = re.compile(r"[0-9a-f]{64}")
CLIENT_STATES = ("running", "paused", "stopped")


class JobRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    max_ram_mb: int = Field(ge=256, le=4_000_000)
    max_threads: int = Field(ge=1, le=1024)
    client_version: str = Field(max_length=20)
    width: int | None = Field(default=None, ge=1, le=10_000)
    height: int | None = Field(default=None, ge=1, le=10_000)
    no_time_limit: bool = False  # the volunteer searches each stage until it is decided
    hunt: bool = False  # only 180-degree symmetric tilings (its "impossible" never settles a size)
    # board, width or symmetric (apps from STRIP_SINCE on); None: an older app, which sends only `hunt`
    search: str | None = Field(default=None, max_length=12)
    # the volunteer's own depths, rising: border depths (the full search always follows), or strip
    # depths for a width search
    custom_stages: list[int] = Field(default=[], max_length=MAX_CUSTOM_STAGES)
    # the volunteer's own time limit per stage, at least the server's; None: the server's
    time_limit_s: float | None = Field(default=None, gt=0, le=LEGACY_UNLIMITED_STAGE_S)


class Heartbeat(BaseModel):
    nonce: str = Field(max_length=64)
    state: str = Field(max_length=10)
    stage_index: int = Field(ge=-1, le=64)
    active_s: float = Field(ge=0, le=1e8)


class Release(BaseModel):
    nonce: str = Field(max_length=64)
    reason: str = Field(max_length=20)
    peak_ram_mb: int = Field(default=0, ge=0, le=MAX_PEAK_RAM_MB)
    detail: str = Field(default="", max_length=200)


class Frame(BaseModel):
    nonce: str = Field(max_length=64)
    stage_index: int = Field(ge=0, le=64)
    placements: list[list[int]] = Field(max_length=100_000)


class Submission(BaseModel):
    job_id: str = Field(max_length=64)
    nonce: str = Field(max_length=64)
    name: str = Field(max_length=40)
    verdict: str = Field(max_length=12)
    stage_index: int = Field(ge=0, le=64)
    duration_s: float = Field(ge=0, le=1e8)
    peak_ram_mb: int = Field(ge=0, le=MAX_PEAK_RAM_MB)
    salt: str = Field(max_length=64)
    token: str = Field(max_length=64)
    client_version: str = Field(max_length=20)
    solution: list[list[int]] | None = Field(default=None, max_length=200_000)
    frame_border: int | None = Field(default=None, ge=1, le=1000)
    frame_placements: list[list[int]] | None = Field(default=None, max_length=100_000)


def version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            return True


def create_app(config: ServerConfig | None = None) -> FastAPI:
    config = config or ServerConfig.from_env()
    db = Database(config.db_path)
    if added := known_strips.seed(db, config.piece.name):
        log.info("added %d known strip results", added)
    scheduler = Scheduler(db, config)
    limiter = RateLimiter(config.max_requests_per_minute)
    pick_lock = threading.Lock()
    page_cache = {"at": 0.0, "html": ""}
    picture_cache: dict[tuple, bytes] = {}
    app = FastAPI(title="fe70", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None:
                return JSONResponse(
                    {"detail": "Content-Length required"}, status_code=411
                )
            if not length.isdigit() or int(length) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "request too large"}, status_code=413)
            if config.access_tokens and request.url.path.startswith("/api/jobs/"):
                given = request.headers.get(TOKEN_HEADER, "")
                if not any(hmac.compare_digest(given, t) for t in config.access_tokens):
                    return JSONResponse(
                        {
                            "detail": "this server needs a valid access token (Advanced > Access token)"
                            + (
                                f"; ask {config.contact} for one"
                                if config.contact
                                else ""
                            )
                        },
                        status_code=401,
                    )
        return await call_next(request)

    def client_ip(request: Request) -> str:
        host = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("cf-connecting-ip")
        if config.trust_proxy_header and forwarded and host in ("127.0.0.1", "::1"):
            return forwarded[:64]
        return host

    def owned_job(job_id: str, nonce: str):
        job = db.job(job_id) if HEX.fullmatch(job_id) else None
        if job is None or not hmac.compare_digest(job.nonce, nonce):
            raise HTTPException(404, "unknown job")
        return job

    def stage_limit(stage: Stage, body: JobRequest) -> float:
        if not body.no_time_limit:
            return body.time_limit_s or stage.time_limit_s
        # apps before NO_LIMIT_SINCE reject a limit of 0; they get the longest limit they accept
        if version_tuple(body.client_version) >= version_tuple(NO_LIMIT_SINCE):
            return NO_LIMIT
        return LEGACY_UNLIMITED_STAGE_S

    @app.post("/api/jobs/request")
    def request_job(body: JobRequest, request: Request) -> dict:
        ip = client_ip(request)
        if not limiter.allow(ip):
            raise HTTPException(429, "too many requests, slow down")
        if version_tuple(body.client_version) < version_tuple(
            config.min_client_version
        ):
            raise HTTPException(
                426,
                f"this client is too old; download version {config.min_client_version} or newer",
            )
        name = body.name.strip()
        if not name or not name.isprintable():
            raise HTTPException(422, "invalid name")
        custom = body.custom_stages
        if not all(1 <= b <= MAX_BORDER for b in custom) or any(
            b <= a for a, b in itertools.pairwise(custom)
        ):
            raise HTTPException(422, "custom stages must be rising border depths")
        if (
            body.time_limit_s is not None
            and body.time_limit_s < config.stage_time_limit_s
        ):
            raise HTTPException(
                422,
                f"the time limit must be at least the server's "
                f"{config.stage_time_limit_s / 3600:g} h (or 0 for no limit)",
            )
        if body.search is not None and body.search not in SEARCHES:
            raise HTTPException(422, f"search must be one of {', '.join(SEARCHES)}")
        hunt = body.hunt if body.search is None else body.search == SEARCH_SYMMETRIC
        strip = body.search == SEARCH_WIDTH
        periodic = body.search == SEARCH_FOREVER
        if strip and version_tuple(body.client_version) < version_tuple(STRIP_SINCE):
            raise HTTPException(
                426, f"strip widths need version {STRIP_SINCE} or newer"
            )
        if periodic and version_tuple(body.client_version) < version_tuple(
            PERIODIC_SINCE
        ):
            raise HTTPException(
                426, f"fill-forever searches need version {PERIODIC_SINCE} or newer"
            )
        requested = None
        requested_width = None
        if strip or periodic:
            requested_width = body.width
        elif body.width is not None or body.height is not None:
            if body.width is None or body.height is None:
                raise HTTPException(422, "give both width and height")
            requested = (body.width, body.height)
        with pick_lock:
            try:
                assignment = scheduler.pick(
                    name,
                    ip,
                    body.max_ram_mb,
                    body.max_threads,
                    requested,
                    no_time_limit=body.no_time_limit,
                    hunt=hunt,
                    custom_borders=tuple(body.custom_stages),
                    strip=strip,
                    requested_width=requested_width,
                    periodic=periodic,
                )
            except NoAssignment as error:
                raise HTTPException(404, str(error)) from error
            except RequestRefused as error:
                raise HTTPException(409, str(error)) from error
            job_id, nonce = secrets.token_hex(16), secrets.token_hex(16)
            stages = [
                {
                    "border": s.border,
                    "lookahead": s.lookahead,
                    "time_limit_s": stage_limit(s, body),
                    "symmetric": s.symmetric,
                    "strip_rows": s.strip_rows,
                    "periodic_start": s.periodic_start,
                    "period": s.period,
                }
                for s in assignment.stages
            ]
            db.add_job(
                job_id,
                nonce,
                assignment.width,
                assignment.height,
                name,
                ip,
                assignment.lane,
                stages,
                assignment.threads,
                assignment.est_ram_mb,
                body.client_version,
            )
        log.info(
            "job %s: %dx%d (%s) to %r",
            job_id,
            assignment.width,
            assignment.height,
            assignment.lane,
            name,
        )
        return {
            "job_id": job_id,
            "nonce": nonce,
            "piece": config.piece.name,
            "width": assignment.width,
            "height": assignment.height,
            "stages": stages,
            "threads": assignment.threads,
            "est_ram_mb": assignment.est_ram_mb,
            "lane": assignment.lane,
            "heartbeat_interval_s": config.heartbeat_interval_s,
            "filled_border": 0
            if assignment.lane in (LANE_STRIP, LANE_PERIODIC)
            else db.filled_border(assignment.width, assignment.height, symmetric=hunt),
        }

    @app.post("/api/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, body: Heartbeat) -> dict:
        job = owned_job(job_id, body.nonce)
        if body.state not in CLIENT_STATES:
            raise HTTPException(422, "invalid state")
        if job.state != "active":
            raise HTTPException(410, f"this job is {job.state}")
        if size_closed(job):
            db.end_job(job_id, "cancelled", "decided elsewhere")
            what = (
                f"width {job.width}"
                if job.lane in (LANE_STRIP, LANE_PERIODIC)
                else f"{job.width} x {job.height}"
            )
            return {"cancel": True, "reason": f"{what} was decided by someone else"}
        db.touch_job(job_id, body.state)
        return {"ok": True}

    def size_closed(job) -> bool:
        """Settled for the main search, or, for a hunt job, already searched by another hunt."""
        if job.lane == LANE_STRIP:
            return job.width in scheduler.dead_widths()
        if job.lane == LANE_PERIODIC:
            return (
                job.width in scheduler.dead_widths()
                or job.width in scheduler.live_table()
            )
        key = (job.width, job.height)
        if scheduler.is_dead(key):
            return True
        summary = scheduler.summaries().get(key)
        if summary is not None and summary.decided:
            return True
        if job.lane != LANE_HUNT:
            return False
        hunted = scheduler.summaries(db.results("hunt")).get(key)
        return hunted is not None and hunted.decided

    @app.post("/api/jobs/{job_id}/release")
    def release(job_id: str, body: Release) -> dict:
        job = owned_job(job_id, body.nonce)
        if body.reason not in RELEASE_REASONS:
            raise HTTPException(422, "invalid reason")
        if job.state != "active":
            return {"ok": True}
        db.end_job(job_id, "released", body.reason, body.peak_ram_mb or None)
        log.info("job %s released: %s %s", job_id, body.reason, body.detail)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/frame")
    def frame(job_id: str, body: Frame) -> dict:
        """A border filling, saved as soon as it is found: the size's next search skips that depth even if
        this one is stopped. Checked cell by cell, so it cannot be faked; a filling proves nothing about the
        rectangle, so the worst a bad one could do is skip a stage that would have been feasible anyway."""
        job = owned_job(job_id, body.nonce)
        if job.state != "active":
            raise HTTPException(410, f"this job is {job.state}")
        if body.stage_index < len(job.stages) and job.stages[body.stage_index].get(
            "strip_rows"
        ):
            save_strip_fill(
                job, job.stages[body.stage_index]["strip_rows"], body.placements
            )
            return {"ok": True}
        if (
            body.stage_index >= len(job.stages)
            or not job.stages[body.stage_index]["border"]
        ):
            raise HTTPException(422, "not a border stage of this job")
        border = job.stages[body.stage_index]["border"]
        symmetric = job.stages[body.stage_index].get("symmetric", False)
        try:
            verify_border(config.piece, job.width, job.height, border, body.placements)
            if symmetric:
                verify_symmetric(config.piece, job.width, job.height, body.placements)
        except TilingError as error:
            log.warning("job %s: border filling rejected: %s", job_id, error)
            raise HTTPException(
                422, f"the border filling is not valid: {error}"
            ) from error
        if db.add_frame(
            job_id,
            job.width,
            job.height,
            border,
            job.name,
            body.placements,
            symmetric,
        ):
            page_cache["at"] = 0.0
            log.info(
                "job %s: %dx%d border %d filled", job_id, job.width, job.height, border
            )
        return {"ok": True}

    def save_strip_fill(job, rows: int, placements: list) -> None:
        """A strip filling: checked cell by cell, then kept (the deepest per job) and shown on the page."""
        try:
            verify_strip(config.piece, job.width, rows, placements)
        except TilingError as error:
            log.warning("job %s: strip filling rejected: %s", job.job_id, error)
            raise HTTPException(
                422, f"the strip filling is not valid: {error}"
            ) from error
        if db.add_strip_fill(job.job_id, job.width, rows, job.name, placements):
            page_cache["at"] = 0.0
            log.info("job %s: width %d filled to depth %d", job.job_id, job.width, rows)

    @app.post("/api/jobs/{job_id}/result")
    def result(job_id: str, body: Submission, request: Request) -> dict:
        job = owned_job(job_id, body.nonce)
        if body.job_id != job_id:
            raise HTTPException(422, "job id mismatch")
        if job.state not in ("active", "expired"):
            raise HTTPException(
                409, f"this job is {job.state}; a result can be sent only once"
            )
        if body.name != job.name:
            raise HTTPException(422, "the name does not match the job")
        if body.verdict not in VERDICTS or body.stage_index >= len(job.stages):
            raise HTTPException(422, "invalid verdict or stage")
        if not (HEX.fullmatch(body.salt) and TOKEN_HEX.fullmatch(body.token)):
            raise HTTPException(422, "invalid salt or token format")
        stage = job.stages[body.stage_index]
        strip = job.lane == LANE_STRIP
        periodic = job.lane == LANE_PERIODIC
        if periodic and body.verdict == VERDICT_FILLED:
            raise HTTPException(422, "a fill-forever search has no 'filled' verdict")
        if strip and body.verdict == VERDICT_POSSIBLE:
            raise HTTPException(422, "a strip search has no tiling to send")
        if (body.verdict == VERDICT_FILLED) and (
            not strip or body.stage_index != len(job.stages) - 1
        ):
            raise HTTPException(
                422, "'filled' only ends a strip search after its last depth"
            )
        if (body.verdict == VERDICT_POSSIBLE) != (body.solution is not None):
            raise HTTPException(
                422, "a solution goes with, and only with, a 'possible' verdict"
            )
        if body.verdict == VERDICT_POSSIBLE and stage["border"] != 0:
            raise HTTPException(422, "a tiling can only come from the full model")
        payload = token.signing_payload(
            job_id,
            job.nonce,
            job.name,
            job.width,
            job.height,
            body.verdict,
            body.stage_index,
            body.duration_s,
            body.salt,
            body.solution,
        )
        if not token.check(config.submit_key, payload, body.token):
            log.warning("job %s: bad token from %s", job_id, client_ip(request))
            raise HTTPException(403, "result signature is not valid")
        if not db.use_salt(body.salt):
            raise HTTPException(409, "this result number was already used")
        elapsed = time.time() - job.created
        if body.duration_s > elapsed + DURATION_SLACK_S:
            raise HTTPException(
                422, "the reported duration is longer than the job has existed"
            )
        limits = [s["time_limit_s"] for s in job.stages[: body.stage_index + 1]]
        unlimited = NO_LIMIT in limits
        if body.verdict == VERDICT_TIMED_OUT and (
            limits[-1] == NO_LIMIT
            or body.duration_s < TIMEOUT_MIN_FRACTION * limits[-1]
        ):
            raise HTTPException(422, "a timeout must last the stage's time limit")
        if (
            body.verdict == VERDICT_IMPOSSIBLE
            and not unlimited
            and body.duration_s > sum(limits) + IMPOSSIBLE_SLACK_S
        ):
            raise HTTPException(422, "the reported duration exceeds the stage limits")
        if strip:
            return strip_result(job, body, stage)
        if periodic:
            return periodic_result(job, body)
        if body.verdict == VERDICT_POSSIBLE:
            try:
                verify_tiling(config.piece, job.width, job.height, body.solution)
            except TilingError as error:
                log.warning("job %s: invalid tiling: %s", job_id, error)
                raise HTTPException(422, f"the tiling is not valid: {error}") from error
        tag = Stage.from_dict(stage).tag
        flagged = body.verdict == VERDICT_IMPOSSIBLE and scheduler.is_suspiciously_fast(
            job.width, job.height, tag, body.duration_s
        )
        picture_border, picture = picture_of(job, body)
        db.add_result(
            picture_border=picture_border,
            picture=picture,
            job_id=job_id,
            width=job.width,
            height=job.height,
            verdict=body.verdict,
            stage_tag=tag,
            duration_s=body.duration_s,
            name=job.name,
            ip=job.ip,
            threads=job.threads,
            peak_ram_mb=body.peak_ram_mb,
            est_ram_mb=job.est_ram_mb,
            client_version=body.client_version,
            flagged=flagged,
            flag_reason="much faster than expected" if flagged else "",
            hard=False,
            source="volunteer",
            solution=body.solution,
        )
        db.end_job(job_id, "done", body.verdict, body.peak_ram_mb)
        page_cache["at"] = 0.0
        log.info(
            "job %s: %dx%d %s by %r (%.0f s)%s",
            job_id,
            job.width,
            job.height,
            body.verdict,
            job.name,
            body.duration_s,
            " FLAGGED" if flagged else "",
        )
        if body.verdict == VERDICT_POSSIBLE:
            log.warning("TILING FOUND: %dx%d by %r", job.width, job.height, job.name)
        message = (
            "Thank you!"
            if not flagged
            else "Recorded; it was unusually fast, so someone will double-check it."
        )
        return {"ok": True, "message": message}

    def strip_result(job, body: Submission, stage: dict) -> dict:
        rows = stage["strip_rows"]
        depths = [s["strip_rows"] for s in job.stages]
        if body.frame_border in depths and body.frame_placements is not None:
            try:  # the last filling, if the heartbeats could not send it on the way
                save_strip_fill(job, body.frame_border, body.frame_placements)
            except HTTPException:
                pass  # already logged; the result itself still counts
        db.add_strip_result(
            job_id=job.job_id,
            width=job.width,
            rows=rows,
            verdict=body.verdict,
            duration_s=body.duration_s,
            name=job.name,
            ip=job.ip,
            threads=job.threads,
            peak_ram_mb=body.peak_ram_mb,
            client_version=body.client_version,
            source="volunteer",
        )
        db.end_job(job.job_id, "done", body.verdict, body.peak_ram_mb)
        page_cache["at"] = 0.0
        log.info(
            "job %s: width %d %s at depth %d by %r (%.0f s)",
            job.job_id,
            job.width,
            body.verdict,
            rows,
            job.name,
            body.duration_s,
        )
        if body.verdict == VERDICT_IMPOSSIBLE:
            log.warning(
                "WIDTH RULED OUT: %d (depth %d) by %r", job.width, rows, job.name
            )
        return {"ok": True, "message": "Thank you!"}

    def periodic_result(job, body: Submission) -> dict:
        """Every stage before the reported one found no repeating filling; the reported one found a
        certificate (checked here cell by cell), found none, or timed out."""
        index = body.stage_index
        found = None
        if body.verdict == VERDICT_POSSIBLE:
            stage = job.stages[index]
            try:
                verify_periodic(
                    config.piece,
                    job.width,
                    stage["periodic_start"],
                    stage["period"],
                    body.solution,
                )
            except TilingError as error:
                log.warning("job %s: repeating filling rejected: %s", job.job_id, error)
                raise HTTPException(
                    422, f"the repeating filling is not valid: {error}"
                ) from error
            found = body.solution
        outcomes = {
            VERDICT_POSSIBLE: PERIODIC_FOUND,
            VERDICT_IMPOSSIBLE: PERIODIC_NONE,
            VERDICT_TIMED_OUT: PERIODIC_TIMED_OUT,
        }
        for i, stage in enumerate(job.stages[: index + 1]):
            last = i == index
            db.add_periodic_result(
                job_id=job.job_id,
                width=job.width,
                start=stage["periodic_start"],
                period=stage["period"],
                verdict=outcomes[body.verdict] if last else PERIODIC_NONE,
                duration_s=body.duration_s if last else 0.0,
                name=job.name,
                ip=job.ip,
                client_version=body.client_version,
                placements=found if last else None,
            )
        db.end_job(job.job_id, "done", body.verdict, body.peak_ram_mb)
        page_cache["at"] = 0.0
        log.info(
            "job %s: width %d periodic %s at stage %d by %r (%.0f s)",
            job.job_id,
            job.width,
            body.verdict,
            index,
            job.name,
            body.duration_s,
        )
        if found is not None:
            log.warning("WIDTH FILLS FOREVER: %d by %r", job.width, job.name)
        return {"ok": True, "message": "Thank you!"}

    def picture_of(job, body: Submission) -> tuple[int | None, list | None]:
        """The tiling itself, or the deepest border filling if it is genuine (display only)."""
        if body.verdict == VERDICT_POSSIBLE:
            return 0, body.solution
        if body.frame_border is None or body.frame_placements is None:
            return None, None
        if body.frame_border not in [s["border"] for s in job.stages]:
            return None, None
        hunt = job.lane == LANE_HUNT
        if body.frame_border <= db.filled_border(job.width, job.height, symmetric=hunt):
            return (
                None,
                None,
            )  # already saved on the way, by this search or an earlier one
        try:
            verify_border(
                config.piece,
                job.width,
                job.height,
                body.frame_border,
                body.frame_placements,
            )
            if hunt:
                verify_symmetric(
                    config.piece, job.width, job.height, body.frame_placements
                )
        except TilingError as error:
            log.warning("job %s: border picture rejected: %s", job.job_id, error)
            return None, None
        return body.frame_border, body.frame_placements

    def png(key: tuple, found: tuple[int, int, list, bool] | None) -> Response:
        if key not in picture_cache:
            if found is None:
                raise HTTPException(404, "no picture")
            if len(picture_cache) >= PICTURE_CACHE_SIZE:
                picture_cache.pop(next(iter(picture_cache)))
            picture_cache[key] = render_png(config.piece, *found)
        return Response(
            picture_cache[key],
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/picture/{result_id}.png")
    def picture_png(result_id: int) -> Response:
        key = ("picture", result_id)
        return png(key, None if key in picture_cache else db.picture(result_id))

    @app.get("/frame/{frame_id}.png")
    def frame_png(frame_id: int) -> Response:
        found = db.frame(frame_id)
        if found is None:
            raise HTTPException(404, "no picture")
        border, *picture = found
        # a deeper filling from the same search replaces this one, hence the depth in the key
        response = png(("frame", frame_id, border), tuple(picture))
        response.headers["Cache-Control"] = "public, max-age=300"
        return response

    @app.get("/strip/{fill_id}.png")
    def strip_png(fill_id: int) -> Response:
        found = db.strip_fill(fill_id)
        if found is None:
            raise HTTPException(404, "no picture")
        width, rows, placements = found
        height = rows + config.piece.strip_reach
        response = png(("strip", fill_id, rows), (width, height, placements))
        response.headers["Cache-Control"] = "public, max-age=300"
        return response

    @app.get("/periodic/{result_id}.png")
    def periodic_png(result_id: int) -> Response:
        found = db.periodic_certificate(result_id)
        if found is None:
            raise HTTPException(404, "no picture")
        width, start, period, placements = found
        block = [p for p in placements if start <= p[2] < start + period]
        unrolled = placements + [
            [o, x, y + copy * period] for copy in (1, 2) for o, x, y in block
        ]
        height = start + 3 * period + config.piece.strip_reach
        return png(("periodic", result_id), (width, height, unrolled))

    @app.get("/api/strips")
    def strips() -> dict:
        states = scheduler.width_states()
        return {
            "widths": [
                {
                    "width": s.width,
                    "dead_rows": s.dead_rows,
                    "dead_by": s.dead_by,
                    "filled_rows": s.filled_rows,
                    "filled_by": s.filled_by,
                    "timeouts": list(s.timeouts),
                }
                for s in states.values()
            ],
            "results": [
                {
                    "width": r.width,
                    "rows": r.rows,
                    "verdict": r.verdict,
                    "duration_s": r.duration_s,
                    "name": r.name,
                    "submitted": r.submitted,
                    "source": r.source,
                }
                for r in db.strip_results()
            ],
            "forever": [
                {
                    "width": c.width,
                    "start": c.start,
                    "period": c.period,
                    "by": c.name,
                    "picture": f"/periodic/{c.result_id}.png",
                }
                for c in scheduler.certified_widths().values()
            ],
        }

    @app.get("/api/results")
    def results() -> list[dict]:
        return [
            {
                "width": r.width,
                "height": r.height,
                "verdict": r.verdict,
                "stage": r.stage_tag,
                "duration_s": r.duration_s,
                "name": r.name,
                "threads": r.threads,
                "submitted": r.submitted,
                "flagged": r.flagged,
                "source": r.source,
                "hunt": r.stage_tag.startswith(SYMMETRIC_TAG_PREFIX),
            }
            for r in db.results("all")
        ]

    @app.get("/api/solution/{width}x{height}")
    def solution(width: int, height: int) -> dict:
        found = db.solution(width, height) or db.solution(height, width)
        if found is None:
            raise HTTPException(404, "no tiling known for this size")
        return {
            "piece": config.piece.name,
            "orientations": config.piece.orientations,
            "placements": found,
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        if time.monotonic() - page_cache["at"] > PAGE_CACHE_S:
            page_cache["html"] = page.render(db, scheduler, config)
            page_cache["at"] = time.monotonic()
        return page_cache["html"]

    @app.get("/api/update")
    def update_manifest() -> dict:
        """latest.json from the download folder, written and signed by scripts/release.py on the maintainer's
        PC; the server cannot forge it (clients check the signatures)."""
        manifest = page.load_manifest(config.download_dir)
        if manifest is None:
            raise HTTPException(404, "no release published")
        return manifest

    @app.get("/download/{filename}")
    def download(filename: str):
        folder = config.download_dir
        if folder is None or not folder.is_dir():
            raise HTTPException(404, "no downloads")
        files = {p.name: p for p in folder.iterdir() if p.is_file()}
        if filename not in files:
            raise HTTPException(404, "no such file")
        return FileResponse(Path(files[filename]), filename=filename)

    return app
