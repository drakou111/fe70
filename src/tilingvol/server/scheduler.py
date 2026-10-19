"""Which size to hand out next, how many threads it gets, and how much RAM it should need.

Two lanes, as in the research sweep: "area" (smallest area first, raises the area bound) and "battle"
(nearest to slow settled misses, which on the sibling piece were the only misses next to tileable sizes).
Timed-out sizes do not steer the battle lane, so it cannot chase its own timeouts."""

import math
import re
import statistics
import time
from dataclasses import dataclass, field, replace

from tilingvol.server.config import ServerConfig
from tilingvol.server.db import Database, ResultRow
from tilingvol.shared.protocol import (
    LANE_HUNT,
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
    Stage,
)

LANE_AREA = "area"
LANE_BATTLE = "battle"
LANE_REQUESTED = "requested"
LANE_RETRY = (
    "retry"  # a size that timed out before, for a volunteer without a time limit
)

# peak RAM ~ BASE + cells * (PER_CELL + PER_CELL_THREAD * threads) MB. Measured: the model alone is about
# 2 GB at 150 x 170; per-thread memory grows over hours (the research sweep peaked at 14 GB with 10 threads
# at 170 x 200). Scaled up automatically when volunteers report higher peaks.
RAM_BASE_MB = 300.0
RAM_PER_CELL_MB = 0.075
RAM_PER_CELL_THREAD_MB = 0.035
RAM_FILL = 0.7  # use at most this share of the volunteer's cap; the client's CP-SAT soft limit sits at 0.75
MEMORY_RETRY_FACTOR = 1.5
MIN_TREND_ROWS = 8
RAM_FACTOR_MIN, RAM_FACTOR_MAX = 0.6, 3.0
LONG_RUN_S = 3600.0
MIN_LONG_RUNS = 20


class NoAssignment(Exception):
    """Nothing can be handed out (HTTP 404)."""


class RequestRefused(Exception):
    """The request itself is unacceptable (HTTP 409)."""


@dataclass(frozen=True)
class Assignment:
    width: int
    height: int
    lane: str
    threads: int
    est_ram_mb: int
    stages: tuple[Stage, ...] = ()


@dataclass
class SizeSummary:
    decided: bool = False
    verdict: str = ""  # best verdict: possible > impossible > timed_out > flagged
    duration_s: float = 0.0
    timed_out: bool = False
    flagged: bool = False
    flagged_by: set = field(default_factory=set)


def percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[int(0.95 * (len(ordered) - 1))]


def fit_trend(rows: list[tuple[int, float]]) -> tuple[float, float, float] | None:
    """Least squares of log(seconds) on log(area): (mean_x, mean_y, slope)."""
    if len(rows) < MIN_TREND_ROWS:
        return None
    xs = [math.log(a) for a, _ in rows]
    ys = [math.log(max(t, 1.0)) for _, t in rows]
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    return mean_x, mean_y, slope


def trend_seconds(trend: tuple[float, float, float], area: int) -> float:
    mean_x, mean_y, slope = trend
    return math.exp(mean_y + slope * (math.log(area) - mean_x))


FRAME_TAG = re.compile(r"frame(\d+)")


def settling_depths(results: list[ResultRow]) -> dict[tuple[int, int], int]:
    """Border depth that proved each size impossible (sizes settled by a border search only)."""
    depths: dict[tuple[int, int], int] = {}
    for r in results:
        match = FRAME_TAG.match(r.stage_tag)
        if r.verdict != VERDICT_IMPOSSIBLE or r.flagged or not match:
            continue
        key = (min(r.width, r.height), max(r.width, r.height))
        depths[key] = min(depths.get(key, 10**9), int(match[1]))
    return depths


class Scheduler:
    def __init__(self, db: Database, config: ServerConfig) -> None:
        self.db = db
        self.config = config
        self.piece = config.piece
        self.sizes = sorted(
            config.piece.candidate_sizes(), key=lambda k: (k[0] * k[1], k)
        )

    # status
    def summaries(
        self, results: list[ResultRow] | None = None
    ) -> dict[tuple[int, int], SizeSummary]:
        out: dict[tuple[int, int], SizeSummary] = {}
        for r in results if results is not None else self.db.results():
            s = out.setdefault(
                (min(r.width, r.height), max(r.width, r.height)), SizeSummary()
            )
            if r.flagged:
                s.flagged = True
                s.flagged_by.update(v for v in (r.name, r.ip) if v)
                continue
            if r.verdict == VERDICT_POSSIBLE:
                s.decided, s.verdict, s.duration_s = (
                    True,
                    VERDICT_POSSIBLE,
                    r.duration_s,
                )
            elif r.verdict == VERDICT_IMPOSSIBLE and s.verdict != VERDICT_POSSIBLE:
                s.decided, s.verdict, s.duration_s = (
                    True,
                    VERDICT_IMPOSSIBLE,
                    r.duration_s,
                )
            elif r.verdict == VERDICT_TIMED_OUT:
                s.timed_out = True
                if not s.decided:
                    s.verdict, s.duration_s = (
                        VERDICT_TIMED_OUT,
                        max(s.duration_s, r.duration_s),
                    )
        return out

    def trends(self, results: list[ResultRow]) -> dict[str, tuple[float, float, float]]:
        by_tag: dict[str, list[tuple[int, float]]] = {}
        for r in results:
            if (
                r.verdict == VERDICT_IMPOSSIBLE
                and not r.flagged
                and not r.hard
                and r.duration_s > 0
            ):
                by_tag.setdefault(r.stage_tag, []).append(
                    (r.width * r.height, r.duration_s)
                )
        return {
            tag: t for tag, rows in by_tag.items() if (t := fit_trend(rows)) is not None
        }

    def battle_seeds(self, results: list[ResultRow]) -> list[tuple[int, int]]:
        trends = self.trends(results)
        seeds = []
        for r in results:
            if r.verdict != VERDICT_IMPOSSIBLE or r.flagged:
                continue
            key = (min(r.width, r.height), max(r.width, r.height))
            if r.hard or (
                r.stage_tag in trends
                and r.duration_s
                >= self.config.outlier_factor
                * trend_seconds(trends[r.stage_tag], r.width * r.height)
            ):
                seeds.append(key)
        return seeds

    def is_suspiciously_fast(
        self, width: int, height: int, stage_tag: str, duration_s: float
    ) -> bool:
        trend = self.trends(self.db.results()).get(stage_tag)
        if trend is None:
            return False
        return duration_s < self.config.fast_flag_factor * trend_seconds(
            trend, width * height
        )

    # stages
    def stages_for(
        self,
        key: tuple[int, int],
        results: list[ResultRow],
        symmetric: bool = False,
        custom_borders: tuple[int, ...] = (),
    ) -> tuple[Stage, ...]:
        """Adaptive: start at the depth that settled the nearest decided size (|dW| + |dH|, then the more
        similar area), go border_increments steps of border_step deeper, then the full model; or the
        volunteer's own border depths. Either way, border depths someone already filled for this size are
        skipped: a filled border proves nothing. The hunt runs the same stages restricted to symmetric
        tilings, and only symmetric fillings let it skip a depth."""
        planned = (
            self._custom_stages(key, custom_borders)
            if custom_borders
            else self._planned_stages(key, results)
        )
        filled = self.db.filled_border(*key, symmetric=symmetric)
        return tuple(
            replace(s, symmetric=symmetric)
            for s in planned
            if s.border == 0 or s.border > filled
        )

    def _custom_stages(
        self, key: tuple[int, int], borders: tuple[int, ...]
    ) -> tuple[Stage, ...]:
        limit = self.config.stage_time_limit_s
        deepest = min(key) // 2
        kept = sorted({min(b, deepest) for b in borders})
        return tuple(Stage(b, True, limit) for b in kept) + (Stage(0, False, limit),)

    def _planned_stages(
        self, key: tuple[int, int], results: list[ResultRow]
    ) -> tuple[Stage, ...]:
        config = self.config
        if not config.adaptive_borders:
            return config.stages
        depths = settling_depths(results)
        start = config.default_border
        if depths:
            nearest = min(
                depths,
                key=lambda k: (
                    abs(k[0] - key[0]) + abs(k[1] - key[1]),
                    abs(k[0] * k[1] - key[0] * key[1]),
                    k,
                ),
            )
            start = depths[nearest]
        limit = config.stage_time_limit_s
        deepest = min(key) // 2
        borders = sorted(
            {
                min(start + i * config.border_step, deepest)
                for i in range(config.border_increments + 1)
            }
        )
        return tuple(Stage(b, True, limit) for b in borders if b > 0) + (
            Stage(0, False, limit),
        )

    # RAM
    def ram_factor(self, results: list[ResultRow]) -> float:
        """Scale for the RAM formula, learned from volunteers' reported peaks. Up as soon as peaks run high;
        down (to RAM_FACTOR_MIN) only from long runs, since memory keeps growing during a search."""
        reported = [
            (r.peak_ram_mb / r.est_ram_mb, r.duration_s)
            for r in results
            if r.source == "volunteer" and r.peak_ram_mb and r.est_ram_mb
        ]
        if len(reported) < MIN_TREND_ROWS:
            return 1.0
        high = percentile95([ratio for ratio, _ in reported])
        if high > 1.0:
            return min(high, RAM_FACTOR_MAX)
        long_runs = [ratio for ratio, seconds in reported if seconds >= LONG_RUN_S]
        if len(long_runs) < MIN_LONG_RUNS:
            return 1.0
        return max(RAM_FACTOR_MIN, percentile95(long_runs))

    def estimate_ram_mb(
        self, width: int, height: int, threads: int, factor: float
    ) -> int:
        cells = width * height
        return int(
            factor
            * (
                RAM_BASE_MB
                + cells * (RAM_PER_CELL_MB + RAM_PER_CELL_THREAD_MB * threads)
            )
        )

    def fit_threads(
        self,
        width: int,
        height: int,
        max_ram_mb: int,
        max_threads: int,
        factor: float,
        failed_mb: int,
    ) -> tuple[int, int] | None:
        budget = RAM_FILL * max_ram_mb
        for threads in range(max_threads, 0, -1):
            est = max(
                self.estimate_ram_mb(width, height, threads, factor),
                int(failed_mb * MEMORY_RETRY_FACTOR),
            )
            if est <= budget:
                return threads, est
        return None

    # leases
    def expire_leases(self, now: float | None = None) -> None:
        now = now or time.time()
        for job in self.db.active_jobs():
            if now - job.heartbeat > self.config.lease_timeout_s:
                self.db.end_job(job.job_id, "expired", "no heartbeat")
            elif (
                job.idle_since is not None
                and now - job.idle_since > self.config.idle_lease_s
            ):
                self.db.end_job(job.job_id, "expired", f"{job.client_state} too long")

    # picking
    def pick(
        self,
        name: str,
        ip: str,
        max_ram_mb: int,
        max_threads: int,
        requested: tuple[int, int] | None,
        no_time_limit: bool = False,
        hunt: bool = False,
        custom_borders: tuple[int, ...] = (),
    ) -> Assignment:
        self.expire_leases()
        results = self.db.results()
        summaries = self.summaries(results)
        active = self.db.active_jobs()
        if sum(1 for j in active if j.ip == ip) >= self.config.max_active_jobs_per_ip:
            raise RequestRefused("too many sizes are already running from your network")
        # the main search and the hunt keep separate books; a size settled by the main search (or a tiling
        # found by the hunt) is closed for both
        if hunt:
            pool = self.summaries(self.db.results("hunt"))
            pool.update({k: s for k, s in summaries.items() if s.decided})
        else:
            pool = summaries
        leased = {
            (min(j.width, j.height), max(j.width, j.height))
            for j in active
            if (j.lane == LANE_HUNT) == hunt
        }
        factor = self.ram_factor(self.db.results("all"))
        failures = self.db.memory_failures()

        def assign(key: tuple[int, int], lane: str, fit: tuple[int, int]) -> Assignment:
            stages = self.stages_for(key, results, hunt, custom_borders)
            return Assignment(key[0], key[1], LANE_HUNT if hunt else lane, *fit, stages)

        if requested is not None:
            key, fit = self._requested(
                requested, pool, leased, max_ram_mb, max_threads, factor, failures
            )
            return assign(key, LANE_REQUESTED, fit)

        def available(key: tuple[int, int]) -> bool:
            s = pool.get(key)
            if key in leased:
                return False
            if s is None:
                return True
            if s.decided:
                return False
            if s.flagged and (name in s.flagged_by or ip in s.flagged_by):
                return False  # a flagged result is re-checked by somebody else
            return not s.timed_out or self.config.retry_timed_out

        if no_time_limit:
            # sizes that timed out before go to volunteers without a time limit first, smallest first
            for key in self.sizes:
                s = pool.get(key)
                if s is None or not s.timed_out or s.decided or key in leased:
                    continue
                fit = self.fit_threads(
                    *key, max_ram_mb, max_threads, factor, failures.get(key, 0)
                )
                if fit is not None:
                    return assign(key, LANE_RETRY, fit)

        lane = LANE_HUNT if hunt else self._lane(active)
        order = self.sizes
        if lane == LANE_BATTLE:
            seeds = self.battle_seeds(results)
            radius = self.config.near_radius

            def priority(key: tuple[int, int]) -> tuple:
                distance = min(
                    (abs(key[0] - a) + abs(key[1] - b) for a, b in seeds),
                    default=radius + 1,
                )
                return min(distance, radius + 1), key[0] * key[1], key

            order = sorted(order, key=priority)
        for key in order:
            if not available(key):
                continue
            fit = self.fit_threads(
                *key, max_ram_mb, max_threads, factor, failures.get(key, 0)
            )
            if fit is None:
                continue
            return assign(key, lane, fit)
        smallest = min(
            (key for key in self.sizes if available(key)),
            key=lambda k: k[0] * k[1],
            default=None,
        )
        if smallest is None:
            raise NoAssignment("every size in range is decided or running")
        need = self.estimate_ram_mb(*smallest, 1, factor) / RAM_FILL
        raise NoAssignment(
            f"no remaining size fits in {max_ram_mb} MB; the smallest needs about {need:.0f} MB"
        )

    def _lane(self, active) -> str:
        lanes = [j.lane for j in active if j.lane in (LANE_AREA, LANE_BATTLE)]
        battle = lanes.count(LANE_BATTLE)
        return (
            LANE_BATTLE
            if (battle + 1) / (len(lanes) + 1) <= self.config.battle_share
            else LANE_AREA
        )

    def _requested(
        self, requested, summaries, leased, max_ram_mb, max_threads, factor, failures
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        width, height = requested
        key = (min(width, height), max(width, height))
        if not (1 <= key[0] and key[1] <= self.config.request_max_side):
            raise RequestRefused(
                f"sizes are limited to {self.config.request_max_side} per side"
            )
        if not self.piece.admissible(*key):
            raise RequestRefused(f"{width} x {height} is impossible by coloring alone")
        if self.piece.exact_dead(*key):
            raise RequestRefused(
                f"{width} x {height} is already excluded by the exact width results"
            )
        s = summaries.get(key)
        if s is not None and s.decided:
            raise RequestRefused(f"{width} x {height} is already decided ({s.verdict})")
        if key in leased:
            raise RequestRefused(
                f"{width} x {height} is being run by someone right now"
            )
        fit = self.fit_threads(
            *key, max_ram_mb, max_threads, factor, failures.get(key, 0)
        )
        if fit is None:
            raise RequestRefused(
                f"{width} x {height} needs more RAM than {max_ram_mb} MB"
            )
        return key, fit
