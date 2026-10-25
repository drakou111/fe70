"""Small JSON-over-HTTP client for the coordination server (stdlib plus certifi, to keep the executable small)."""

import json
import re
import time
import urllib.error
import urllib.request

from tilingvol import __version__
from tilingvol.client import tls
from tilingvol.shared.pieces import PIECES
from tilingvol.shared.protocol import (
    MAX_PEAK_RAM_MB,
    MAX_STRIP_ROWS,
    SEARCH_BOARD,
    SEARCH_SYMMETRIC,
    Job,
    ResultSubmission,
)

TIMEOUT_S = 30
MAX_RESPONSE_BYTES = 1 << 20


class ServerError(RuntimeError):
    """The server answered with an error; `status` is the HTTP code (0 when it could not be reached)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class NoJobAvailable(ServerError):
    pass


MAX_SIDE = 1000
MAX_STAGES = 8
MAX_STAGE_SECONDS = 7 * 24 * 3600
TOKEN_PATTERN = re.compile(r"[0-9a-f]{16,64}")


def validate_job(job: Job, max_threads: int) -> Job:
    """Never trust the server blindly: a hijacked server must not be able to make the client misbehave."""
    problems = []
    if job.piece not in PIECES:
        problems.append(f"unknown piece {job.piece!r}")
    if not (1 <= job.width <= MAX_SIDE and 1 <= job.height <= MAX_SIDE):
        problems.append("size out of range")
    if not 1 <= job.threads <= max_threads:
        problems.append(f"{job.threads} threads is more than the {max_threads} allowed")
    if not 1 <= len(job.stages) <= MAX_STAGES or job.stages[-1].border != 0:
        problems.append("bad stage list")
    if job.strip:
        depths = [stage.strip_rows for stage in job.stages]
        if not (
            all(1 <= d <= MAX_STRIP_ROWS for d in depths)
            and depths == sorted(set(depths))
            and job.height == depths[-1]
            and not any(stage.border or stage.symmetric for stage in job.stages)
        ):
            problems.append("bad strip depths")
    for stage in job.stages:
        if not (
            0 <= stage.border <= min(job.width, job.height) // 2
            and (stage.unlimited or 1 <= stage.time_limit_s <= MAX_STAGE_SECONDS)
        ):
            problems.append("bad stage")
    if not (TOKEN_PATTERN.fullmatch(job.job_id) and TOKEN_PATTERN.fullmatch(job.nonce)):
        problems.append("bad job id")
    if not 0 <= job.filled_border <= min(job.width, job.height) // 2:
        problems.append("bad filled border")
    if not 0 < job.heartbeat_interval_s <= 3600:
        problems.append("bad heartbeat interval")
    if problems:
        raise ServerError(502, "the server sent an invalid job: " + "; ".join(problems))
    return job


class ApiClient:
    def __init__(self, base_url: str, access_token: str = "") -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"server URL must start with http:// or https://, got {base_url!r}"
            )
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.last_reached = (
            0.0  # time.time() of the last answer from the server (errors count)
        )

    def request_job(
        self,
        name: str,
        max_ram_mb: int,
        max_threads: int,
        width: int | None = None,
        height: int | None = None,
        no_time_limit: bool = False,
        search: str = SEARCH_BOARD,
        custom_borders: tuple[int, ...] = (),
        time_limit_s: float | None = None,
    ) -> Job:
        body = {
            "name": name,
            "max_ram_mb": max_ram_mb,
            "max_threads": max_threads,
            "client_version": __version__,
            "width": width,
            "height": height,
            "no_time_limit": no_time_limit,
            "time_limit_s": time_limit_s,
            "search": search,
            "hunt": search
            == SEARCH_SYMMETRIC,  # servers before 0.10 only know this flag
            "custom_stages": list(custom_borders),
        }
        try:
            data = self._post("/api/jobs/request", body)
        except ServerError as error:
            if error.status == 404:
                raise NoJobAvailable(error.status, str(error)) from error
            raise
        try:
            job = Job.from_dict(data)
        except (KeyError, TypeError, ValueError) as error:
            raise ServerError(502, f"malformed job from the server: {error}") from error
        return validate_job(job, max_threads)

    def heartbeat(
        self, job: Job, state: str, stage_index: int, active_s: float
    ) -> dict:
        return self._post(
            f"/api/jobs/{job.job_id}/heartbeat",
            {
                "nonce": job.nonce,
                "state": state,
                "stage_index": stage_index,
                "active_s": active_s,
            },
        )

    def release(
        self, job: Job, reason: str, peak_ram_mb: int = 0, detail: str = ""
    ) -> None:
        self._post(
            f"/api/jobs/{job.job_id}/release",
            {
                "nonce": job.nonce,
                "reason": reason,
                "peak_ram_mb": min(max(peak_ram_mb, 0), MAX_PEAK_RAM_MB),
                "detail": detail,
            },
        )

    def send_frame(self, job: Job, stage_index: int, placements: list) -> None:
        self._post(
            f"/api/jobs/{job.job_id}/frame",
            {"nonce": job.nonce, "stage_index": stage_index, "placements": placements},
        )

    def submit(self, submission: ResultSubmission) -> dict:
        return self._post(f"/api/jobs/{submission.job_id}/result", submission.to_dict())

    def get_json(self, path: str) -> dict:
        request = urllib.request.Request(
            self.base_url + path, headers={"User-Agent": f"tilingvol/{__version__}"}
        )
        return self._send(request)

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"tilingvol/{__version__}",
                **({"X-Access-Token": self.access_token} if self.access_token else {}),
            },
        )
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> dict:
        try:
            with tls.urlopen(request, timeout=TIMEOUT_S) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            self.last_reached = time.time()
        except urllib.error.HTTPError as error:
            self.last_reached = time.time()
            try:
                detail = json.loads(error.read(MAX_RESPONSE_BYTES)).get(
                    "detail", error.reason
                )
            except (json.JSONDecodeError, AttributeError):
                detail = error.reason
            raise ServerError(error.code, str(detail)[:300]) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise ServerError(0, f"cannot reach the server: {error}") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ServerError(502, "the server reply is too large")
        try:
            reply = json.loads(raw or b"{}")
        except json.JSONDecodeError as error:
            raise ServerError(502, f"the server reply is not JSON: {error}") from error
        if not isinstance(reply, dict):
            raise ServerError(502, "the server reply is not a JSON object")
        return reply
