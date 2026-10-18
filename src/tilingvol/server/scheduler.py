"""Which size to hand out next, how many threads it gets, and how much RAM it should need.

Two lanes, as in the research sweep: "area" (smallest area first, raises the area bound) and "battle"
(nearest to slow settled misses, which on the sibling piece were the only misses next to tileable sizes).
Timed-out sizes do not steer the battle lane, so it cannot chase its own timeouts."""

import math
import statistics
import time
from dataclasses import dataclass, field

from tilingvol.server.config import ServerConfig
from tilingvol.server.db import Database, ResultRow
from tilingvol.shared.protocol import (
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
)

LANE_AREA = "area"
LANE_BATTLE = "battle"
LANE_REQUESTED = "requested"

# peak RAM ~ BASE + cells * (PER_CELL + PER_CELL_THREAD * threads) MB. Measured: the model alone is about
# 2 GB at 150 x 170; per-thread memory grows over hours (the research sweep peaked at 14 GB with 10 threads
# at 170 x 200). Scaled up automatically when volunteers report higher peaks.
RAM_BASE_MB = 300.0
RAM_PER_CELL_MB = 0.075
RAM_PER_CELL_THREAD_MB = 0.035
RAM_FILL = 0.7  # use at most this share of the volunteer's cap; the client's CP-SAT soft limit sits at 0.75
MEMORY_RETRY_FACTOR = 1.5
MIN_TREND_ROWS = 8


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


@dataclass
class SizeSummary:
    decided: bool = False
    verdict: str = ""  # best verdict: possible > impossible > timed_out > flagged
    duration_s: float = 0.0
    timed_out: bool = False
    flagged: bool = False
    flagged_by: set = field(default_factory=set)


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

    # RAM
    def ram_factor(self, results: list[ResultRow]) -> float:
        ratios = sorted(
            r.peak_ram_mb / r.est_ram_mb
            for r in results
            if r.source == "volunteer" and r.peak_ram_mb and r.est_ram_mb
        )
        if len(ratios) < MIN_TREND_ROWS:
            return 1.0
        return max(1.0, ratios[int(0.95 * (len(ratios) - 1))])

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
    ) -> Assignment:
        self.expire_leases()
        results = self.db.results()
        summaries = self.summaries(results)
        active = self.db.active_jobs()
        leased = {(min(j.width, j.height), max(j.width, j.height)) for j in active}
        if sum(1 for j in active if j.ip == ip) >= self.config.max_active_jobs_per_ip:
            raise RequestRefused("too many sizes are already running from your network")
        factor = self.ram_factor(results)
        failures = self.db.memory_failures()

        if requested is not None:
            return self._requested(
                requested, summaries, leased, max_ram_mb, max_threads, factor, failures
            )

        def available(key: tuple[int, int]) -> bool:
            s = summaries.get(key)
            if key in leased:
                return False
            if s is None:
                return True
            if s.decided:
                return False
            if s.flagged and (name in s.flagged_by or ip in s.flagged_by):
                return False  # a flagged result is re-checked by somebody else
            return not s.timed_out or self.config.retry_timed_out

        lane = self._lane(active)
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
            return Assignment(key[0], key[1], lane, *fit)
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
    ) -> Assignment:
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
        return Assignment(key[0], key[1], LANE_REQUESTED, *fit)
