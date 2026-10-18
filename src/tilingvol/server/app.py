"""Coordination API. Run with: uvicorn --factory tilingvol.server.app:create_app --host 127.0.0.1 --port 8000

Security notes: listen on 127.0.0.1 only (cloudflared forwards to it); every body is size-capped and
validated by pydantic; nothing from a request is executed, unpickled or passed to a shell; SQL is
parameterized; names are HTML-escaped on the page."""

import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from tilingvol.server import page
from tilingvol.server.config import ServerConfig
from tilingvol.server.db import Database
from tilingvol.server.scheduler import NoAssignment, RequestRefused, Scheduler
from tilingvol.shared import token
from tilingvol.shared.protocol import (
    RELEASE_REASONS,
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
    VERDICTS,
    Stage,
)
from tilingvol.shared.verify import TilingError, verify_tiling

log = logging.getLogger("tilingvol.server")

MAX_BODY_BYTES = 4 << 20
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


class Heartbeat(BaseModel):
    nonce: str = Field(max_length=64)
    state: str = Field(max_length=10)
    stage_index: int = Field(ge=-1, le=64)
    active_s: float = Field(ge=0, le=1e8)


class Release(BaseModel):
    nonce: str = Field(max_length=64)
    reason: str = Field(max_length=20)
    peak_ram_mb: int = Field(default=0, ge=0, le=10_000_000)
    detail: str = Field(default="", max_length=200)


class Submission(BaseModel):
    job_id: str = Field(max_length=64)
    nonce: str = Field(max_length=64)
    name: str = Field(max_length=40)
    verdict: str = Field(max_length=12)
    stage_index: int = Field(ge=0, le=64)
    duration_s: float = Field(ge=0, le=1e8)
    peak_ram_mb: int = Field(ge=0, le=10_000_000)
    salt: str = Field(max_length=64)
    token: str = Field(max_length=64)
    client_version: str = Field(max_length=20)
    solution: list[list[int]] | None = Field(default=None, max_length=200_000)


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
    scheduler = Scheduler(db, config)
    limiter = RateLimiter(config.max_requests_per_minute)
    pick_lock = threading.Lock()
    page_cache = {"at": 0.0, "html": ""}
    app = FastAPI(
        title="Tiling volunteer", docs_url=None, redoc_url=None, openapi_url=None
    )
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
        requested = None
        if body.width is not None or body.height is not None:
            if body.width is None or body.height is None:
                raise HTTPException(422, "give both width and height")
            requested = (body.width, body.height)
        with pick_lock:
            try:
                assignment = scheduler.pick(
                    name, ip, body.max_ram_mb, body.max_threads, requested
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
                    "time_limit_s": s.time_limit_s,
                }
                for s in config.stages
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
        }

    @app.post("/api/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, body: Heartbeat) -> dict:
        job = owned_job(job_id, body.nonce)
        if body.state not in CLIENT_STATES:
            raise HTTPException(422, "invalid state")
        if job.state != "active":
            raise HTTPException(410, f"this job is {job.state}")
        key = (job.width, job.height)
        summary = scheduler.summaries().get(key)
        if summary is not None and summary.decided:
            db.end_job(job_id, "cancelled", "decided elsewhere")
            return {
                "cancel": True,
                "reason": f"{job.width} x {job.height} was decided by someone else",
            }
        db.touch_job(job_id, body.state)
        return {"ok": True}

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
        limits = [s["time_limit_s"] for s in job.stages]
        if (
            body.verdict == VERDICT_TIMED_OUT
            and body.duration_s < TIMEOUT_MIN_FRACTION * limits[body.stage_index]
        ):
            raise HTTPException(422, "a timeout must last the stage's time limit")
        if (
            body.verdict == VERDICT_IMPOSSIBLE
            and body.duration_s
            > sum(limits[: body.stage_index + 1]) + IMPOSSIBLE_SLACK_S
        ):
            raise HTTPException(422, "the reported duration exceeds the stage limits")
        if body.verdict == VERDICT_POSSIBLE:
            try:
                verify_tiling(config.piece, job.width, job.height, body.solution)
            except TilingError as error:
                log.warning("job %s: invalid tiling: %s", job_id, error)
                raise HTTPException(422, f"the tiling is not valid: {error}") from error
        tag = Stage(stage["border"], stage["lookahead"], stage["time_limit_s"]).tag
        flagged = body.verdict == VERDICT_IMPOSSIBLE and scheduler.is_suspiciously_fast(
            job.width, job.height, tag, body.duration_s
        )
        db.add_result(
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
            }
            for r in db.results()
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
