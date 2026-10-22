"""Messages exchanged between client and server, as plain dataclasses with dict (JSON) conversion."""

import itertools
from dataclasses import asdict, dataclass

VERDICT_IMPOSSIBLE = "impossible"
VERDICT_TIMED_OUT = "timed_out"
VERDICT_POSSIBLE = "possible"
VERDICT_FILLED = "filled"  # strip jobs only: every depth of the job could be filled
VERDICTS = (VERDICT_IMPOSSIBLE, VERDICT_TIMED_OUT, VERDICT_POSSIBLE, VERDICT_FILLED)

RELEASE_GAVE_UP = "gave_up"
RELEASE_MEMORY = "memory"
RELEASE_CLOSED = "closed"
RELEASE_ERROR = "error"
RELEASE_REASONS = (RELEASE_GAVE_UP, RELEASE_MEMORY, RELEASE_CLOSED, RELEASE_ERROR)


NO_LIMIT = 0.0  # a stage time limit of 0 means: search until the stage is decided

LANE_HUNT = "hunt"  # 180-degree symmetric tilings only; its "impossible" proves nothing about the size
# half-strip depth: can rows 0..h-1 of the width-W half-strip be covered? "impossible" rules out every
# rectangle with a side W (research repo, FIVE_DIVISIBLE_SIDE.md); apps before STRIP_SINCE cannot run it
LANE_STRIP = "strip"
# what a volunteer chose to run (apps from STRIP_SINCE on send it; older ones send only the hunt flag)
SEARCH_BOARD = "board"  # rectangle sizes
SEARCH_WIDTH = "width"  # half-strip widths
SEARCH_SYMMETRIC = "symmetric"  # the symmetric hunt
SEARCHES = (SEARCH_BOARD, SEARCH_WIDTH, SEARCH_SYMMETRIC)
STRIP_SINCE = "0.10.0"
MAX_STRIP_ROWS = 1000
SYMMETRIC_TAG_PREFIX = "sym"
MAX_CUSTOM_STAGES = 8
MAX_BORDER = 1000


def parse_custom_stages(text: str) -> tuple[int, ...]:
    """Border depths typed by the volunteer, e.g. "24, 28 32"; the full search always follows. Empty: the
    server's own stages."""
    parts = text.replace(",", " ").split()
    if not all(p.isdigit() for p in parts):
        raise ValueError(
            "custom stages are whole-number border depths, like 24, 28, 32"
        )
    depths = tuple(int(p) for p in parts)
    if len(depths) > MAX_CUSTOM_STAGES:
        raise ValueError(f"at most {MAX_CUSTOM_STAGES} custom stages")
    if not all(1 <= d <= MAX_BORDER for d in depths):
        raise ValueError(f"border depths must be between 1 and {MAX_BORDER}")
    if any(b <= a for a, b in itertools.pairwise(depths)):
        raise ValueError("custom border depths must go up, like 24, 28, 32")
    return depths


@dataclass(frozen=True)
class Stage:
    border: int  # 0 = the full rectangle model
    lookahead: bool
    time_limit_s: float  # NO_LIMIT = unlimited
    symmetric: bool = (
        False  # only tilings unchanged by a 180-degree turn (the hunt lane)
    )
    strip_rows: int = 0  # > 0: cover the first strip_rows rows of the width-W half-strip (border is 0)

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
        if self.strip_rows:
            return f"strip{self.strip_rows}"
        base = (
            "full"
            if self.border == 0
            else f"frame{self.border}" + ("L" if self.lookahead else "")
        )
        return SYMMETRIC_TAG_PREFIX + base if self.symmetric else base

    def describe(self) -> str:
        if self.strip_rows:
            return f"strip depth {self.strip_rows}"
        text = "full search" if self.border == 0 else f"border depth {self.border}"
        return "symmetric " + text if self.symmetric else text

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Stage":
        return Stage(
            int(data["border"]),
            bool(data["lookahead"]),
            float(data["time_limit_s"]),
            bool(data.get("symmetric", False)),
            int(data.get("strip_rows", 0)),
        )


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

    @property
    def hunt(self) -> bool:
        return any(stage.symmetric for stage in self.stages)

    @property
    def strip(self) -> bool:
        """A half-strip depth job of width `width`; `height` is its deepest depth."""
        return any(stage.strip_rows for stage in self.stages)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Job":
        stages = tuple(Stage.from_dict(s) for s in data["stages"])
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
