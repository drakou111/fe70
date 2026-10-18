"""Messages exchanged between client and server, as plain dataclasses with dict (JSON) conversion."""

from dataclasses import asdict, dataclass

VERDICT_IMPOSSIBLE = "impossible"
VERDICT_TIMED_OUT = "timed_out"
VERDICT_POSSIBLE = "possible"
VERDICTS = (VERDICT_IMPOSSIBLE, VERDICT_TIMED_OUT, VERDICT_POSSIBLE)

RELEASE_GAVE_UP = "gave_up"
RELEASE_MEMORY = "memory"
RELEASE_CLOSED = "closed"
RELEASE_ERROR = "error"
RELEASE_REASONS = (RELEASE_GAVE_UP, RELEASE_MEMORY, RELEASE_CLOSED, RELEASE_ERROR)


NO_LIMIT = 0.0  # a stage time limit of 0 means: search until the stage is decided


@dataclass(frozen=True)
class Stage:
    border: int  # 0 = the full rectangle model
    lookahead: bool
    time_limit_s: float  # NO_LIMIT = unlimited

    @property
    def unlimited(self) -> bool:
        return self.time_limit_s == NO_LIMIT

    def describe_limit(self) -> str:
        if self.unlimited:
            return "no limit"
        seconds = int(self.time_limit_s)
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    @property
    def tag(self) -> str:
        if self.border == 0:
            return "full"
        return f"frame{self.border}" + ("L" if self.lookahead else "")

    def describe(self) -> str:
        return "full search" if self.border == 0 else f"border depth {self.border}"


@dataclass(frozen=True)
class Job:
    job_id: str
    nonce: str
    piece: str
    width: int
    height: int
    stages: tuple[Stage, ...]
    threads: int
    est_ram_mb: int
    lane: str
    heartbeat_interval_s: float
    filled_border: int = 0  # deepest border an earlier search of this size filled; the stages start deeper

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Job":
        stages = tuple(
            Stage(int(s["border"]), bool(s["lookahead"]), float(s["time_limit_s"]))
            for s in data["stages"]
        )
        return Job(
            job_id=str(data["job_id"]),
            nonce=str(data["nonce"]),
            piece=str(data["piece"]),
            width=int(data["width"]),
            height=int(data["height"]),
            stages=stages,
            threads=int(data["threads"]),
            est_ram_mb=int(data["est_ram_mb"]),
            lane=str(data["lane"]),
            heartbeat_interval_s=float(data["heartbeat_interval_s"]),
            filled_border=int(data.get("filled_border", 0)),
        )


@dataclass(frozen=True)
class ResultSubmission:
    job_id: str
    nonce: str
    name: str
    verdict: str
    stage_index: int
    duration_s: float
    peak_ram_mb: int
    salt: str
    token: str
    client_version: str
    solution: list[list[int]] | None = None
    # the deepest border filling found on the way (display only; not part of the signature)
    frame_border: int | None = None
    frame_placements: list[list[int]] | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "ResultSubmission":
        solution = data.get("solution")
        return ResultSubmission(
            job_id=str(data["job_id"]),
            nonce=str(data["nonce"]),
            name=str(data["name"]),
            verdict=str(data["verdict"]),
            stage_index=int(data["stage_index"]),
            duration_s=float(data["duration_s"]),
            peak_ram_mb=int(data["peak_ram_mb"]),
            salt=str(data["salt"]),
            token=str(data["token"]),
            client_version=str(data["client_version"]),
            solution=[[int(v) for v in p] for p in solution]
            if solution is not None
            else None,
            frame_border=data.get("frame_border"),
            frame_placements=data.get("frame_placements"),
        )
