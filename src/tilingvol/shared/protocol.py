"""Messages exchanged between client and server, as plain dataclasses with dict (JSON) conversion."""

import itertools
from dataclasses import asdict, dataclass

VERDICT_IMPOSSIBLE = "impossible"
VERDICT_TIMED_OUT = "timed_out"
VERDICT_POSSIBLE = "possible"
VERDICT_FILLED = "filled"  # strip jobs only: every depth of the job could be filled
VERDICTS = (VERDICT_IMPOSSIBLE, VERDICT_TIMED_OUT, VERDICT_POSSIBLE, VERDICT_FILLED)

# the largest RAM figure the server stores; readings above it are measurement glitches
MAX_PEAK_RAM_MB = 10_000_000

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
# fill-forever proof: a half-strip filling whose rows from `periodic_start` on repeat every `period` rows; a
# found one is checked cell by cell (shared/verify.py), so it settles the width for good
LANE_PERIODIC = "periodic"
# exact width decision (native/exact_width.cpp): every row of the width-W half-strip until it dies (no
# rectangle has a side W), tiles a rectangle, or repeats an earlier row exactly (the width fills forever)
LANE_EXACT = "exact"
# what a volunteer chose to run (apps from STRIP_SINCE on send it; older ones send only the hunt flag)
SEARCH_BOARD = "board"  # rectangle sizes
SEARCH_WIDTH = "width"  # half-strip widths
SEARCH_SYMMETRIC = "symmetric"  # the symmetric hunt
SEARCH_FOREVER = "forever"  # repeating half-strip fillings
SEARCH_EXACT = "exact"  # exact width decisions
SEARCHES = (SEARCH_BOARD, SEARCH_WIDTH, SEARCH_SYMMETRIC, SEARCH_FOREVER, SEARCH_EXACT)
STRIP_SINCE = "0.10.0"
PERIODIC_SINCE = "0.12.0"
EXACT_SINCE = "0.13.0"
EXACT_DEAD = "dead"  # no filling of `row` rows: no rectangle has a side W
EXACT_RECTANGLE = "rectangle"  # the flat frontier at `row`: a W x row rectangle tiles
EXACT_FOREVER = (
    "forever"  # row `row` repeats row `repeat_from` exactly: the width fills forever
)
EXACT_VERDICTS = {
    EXACT_DEAD: VERDICT_IMPOSSIBLE,
    EXACT_RECTANGLE: VERDICT_POSSIBLE,
    EXACT_FOREVER: VERDICT_FILLED,
}
MAX_EXACT_ROW = 100_000
# exact_width --lookahead: rows ahead a frontier must stay fillable to be kept (0 = keep every frontier);
# verdicts are the same for every lookahead, state counts are comparable only between equal ones
EXACT_LOOKAHEAD = 3
MAX_EXACT_LOOKAHEAD = 6
# a piece spans at most 7 rows, so with period >= 7 each repeated block touches only its neighbours
MIN_PERIOD = 7
MAX_PERIOD = 200
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
    periodic_start: int = (
        0  # with period > 0: a half-strip filling repeating from this row on
    )
    period: int = 0
    exact: bool = False  # an exact width decision (no time limit, no CP-SAT)

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
        if self.exact:
            return "exact"
        if self.period:
            return f"periodic{self.periodic_start}p{self.period}"
        if self.strip_rows:
            return f"strip{self.strip_rows}"
        base = (
            "full"
            if self.border == 0
            else f"frame{self.border}" + ("L" if self.lookahead else "")
        )
        return SYMMETRIC_TAG_PREFIX + base if self.symmetric else base

    def describe(self) -> str:
        if self.exact:
            return "exact decision (row by row until it dies, tiles or repeats)"
        if self.period:
            return f"repeating filling (rows from {self.periodic_start} repeat every {self.period})"
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
            int(data.get("periodic_start", 0)),
            int(data.get("period", 0)),
            bool(data.get("exact", False)),
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

    @property
    def periodic(self) -> bool:
        """A fill-forever job of width `width`; `height` is the most rows any of its stages builds."""
        return any(stage.period for stage in self.stages)

    @property
    def exact(self) -> bool:
        """An exact decision of width `width`; `height` is unused (0)."""
        return any(stage.exact for stage in self.stages)

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
class ExactOutcome:
    """How an exact decision ended. counts: the minimal DFA's state count of every row from the first one
    the tool computed (equal runs give equal counts, so two volunteers' runs can be compared)."""

    outcome: str
    row: int
    repeat_from: int  # EXACT_FOREVER only, else 0
    first_row: int
    counts: tuple[int, ...]
    compute_s: float  # every run of this width on this computer, resumed ones included
    lookahead: int = 0  # the run's --lookahead (a resumed run keeps its checkpoint's)

    def __post_init__(self) -> None:
        if self.outcome not in EXACT_VERDICTS:
            raise ValueError(f"unknown exact outcome {self.outcome!r}")
        if not 0 <= self.first_row <= self.row <= MAX_EXACT_ROW:
            raise ValueError("exact rows out of range")
        if len(self.counts) != self.row - self.first_row + 1:
            raise ValueError("one state count per row is expected")
        repeats = self.first_row <= self.repeat_from < self.row
        if self.outcome == EXACT_FOREVER and not repeats:
            raise ValueError("a repeating row must repeat an earlier row")
        if self.outcome != EXACT_FOREVER and self.repeat_from != 0:
            raise ValueError("repeat_from goes only with a repeating row")
        if not 0 <= self.lookahead <= MAX_EXACT_LOOKAHEAD:
            raise ValueError("exact lookahead out of range")

    @property
    def verdict(self) -> str:
        return EXACT_VERDICTS[self.outcome]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["counts"] = list(self.counts)
        if not self.lookahead:
            del data["lookahead"]  # what 0.13.0 and 0.13.1 apps sign
        return data

    @staticmethod
    def from_dict(data: dict) -> "ExactOutcome":
        return ExactOutcome(
            str(data["outcome"]),
            int(data["row"]),
            int(data["repeat_from"]),
            int(data["first_row"]),
            tuple(int(c) for c in data["counts"]),
            float(data["compute_s"]),
            int(data.get("lookahead", 0)),
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
    exact: dict | None = (
        None  # ExactOutcome.to_dict() of an exact job (signed with the result)
    )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["peak_ram_mb"] = min(max(self.peak_ram_mb, 0), MAX_PEAK_RAM_MB)
        return data

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
            exact=data.get("exact"),
        )
