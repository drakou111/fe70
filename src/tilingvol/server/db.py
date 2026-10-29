"""SQLite storage. One connection guarded by a lock (the API runs in a single process); every query is
parameterized."""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from tilingvol.shared.protocol import SYMMETRIC_TAG_PREFIX

# results of the symmetric hunt; only a tiling found there (verdict "possible") also counts for the size
HUNT_ROW = f"stage_tag LIKE '{SYMMETRIC_TAG_PREFIX}%'"
MAIN_ROW = f"(NOT {HUNT_ROW} OR verdict = 'possible')"
RESULT_KINDS = {"main": MAIN_ROW, "hunt": HUNT_ROW, "all": "1"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    name TEXT NOT NULL,
    ip TEXT NOT NULL,
    lane TEXT NOT NULL,
    stages TEXT NOT NULL,
    threads INTEGER NOT NULL,
    est_ram_mb INTEGER NOT NULL,
    client_version TEXT NOT NULL,
    created REAL NOT NULL,
    heartbeat REAL NOT NULL,
    client_state TEXT NOT NULL DEFAULT 'running',
    idle_since REAL,
    state TEXT NOT NULL,          -- active, done, released, expired, cancelled
    end_reason TEXT,
    peak_ram_mb INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    stage_tag TEXT NOT NULL,
    duration_s REAL NOT NULL,
    name TEXT NOT NULL,
    ip TEXT,
    threads INTEGER,
    peak_ram_mb INTEGER,
    est_ram_mb INTEGER,
    client_version TEXT,
    submitted REAL NOT NULL,
    flagged INTEGER NOT NULL DEFAULT 0,
    flag_reason TEXT,
    hard INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,          -- volunteer or import
    solution TEXT
);
CREATE INDEX IF NOT EXISTS results_size ON results(width, height);
CREATE TABLE IF NOT EXISTS used_salts (salt TEXT PRIMARY KEY, used REAL NOT NULL);
CREATE TABLE IF NOT EXISTS frames (          -- border fillings, saved the moment they are found
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,              -- only the deepest one per job is kept
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    border INTEGER NOT NULL,
    name TEXT NOT NULL,
    submitted REAL NOT NULL,
    placements TEXT NOT NULL,
    symmetric INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS frames_size ON frames(width, height);
CREATE TABLE IF NOT EXISTS strip_results (   -- half-strip depth searches; rows = the depth of the verdict
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    width INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    verdict TEXT NOT NULL,                    -- impossible (width ruled out), timed_out, filled
    duration_s REAL NOT NULL,
    name TEXT NOT NULL,
    ip TEXT,
    threads INTEGER,
    peak_ram_mb INTEGER,
    client_version TEXT,
    submitted REAL NOT NULL,
    source TEXT NOT NULL                      -- volunteer or import
);
CREATE INDEX IF NOT EXISTS strip_results_width ON strip_results(width);
CREATE TABLE IF NOT EXISTS strip_fills (     -- the deepest filling of each strip job, saved when found
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    width INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    name TEXT NOT NULL,
    submitted REAL NOT NULL,
    placements TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strip_fills_width ON strip_fills(width);
CREATE TABLE IF NOT EXISTS periodic_results (  -- fill-forever searches, one row per (start, period) tried
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    width INTEGER NOT NULL,
    start INTEGER NOT NULL,
    period INTEGER NOT NULL,
    verdict TEXT NOT NULL,                    -- found (checked certificate), none, timed_out
    duration_s REAL NOT NULL,
    name TEXT NOT NULL,
    ip TEXT,
    client_version TEXT,
    submitted REAL NOT NULL,
    source TEXT NOT NULL,
    placements TEXT                           -- the certificate, for 'found'
);
CREATE INDEX IF NOT EXISTS periodic_results_width ON periodic_results(width);
CREATE TABLE IF NOT EXISTS exact_results (   -- exact width decisions (the client's exact_width program)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    width INTEGER NOT NULL,
    outcome TEXT NOT NULL,                    -- dead, rectangle or forever
    row INTEGER NOT NULL,                     -- the row that decided it
    repeat_from INTEGER NOT NULL,             -- forever: the earlier row it repeats
    first_row INTEGER NOT NULL,
    counts TEXT NOT NULL,                     -- JSON: minimal DFA states per row from first_row
    compute_s REAL NOT NULL,                  -- every run of the width on that computer
    lookahead INTEGER NOT NULL DEFAULT 0,     -- the run's --lookahead; counts compare only between equal ones
    name TEXT NOT NULL,
    ip TEXT,
    peak_ram_mb INTEGER,
    client_version TEXT,
    submitted REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS exact_results_width ON exact_results(width);
CREATE TABLE IF NOT EXISTS exact_progress (  -- the row each running exact decision has reached
    width INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    job_id TEXT NOT NULL,
    row INTEGER NOT NULL,
    states INTEGER NOT NULL,
    resumable INTEGER NOT NULL,               -- 1: the volunteer saves checkpoints and can resume it
    updated REAL NOT NULL
);
"""


@dataclass(frozen=True)
class JobRow:
    job_id: str
    nonce: str
    width: int
    height: int
    name: str
    ip: str
    lane: str
    stages: list[dict]
    threads: int
    est_ram_mb: int
    created: float
    heartbeat: float
    client_state: str
    idle_since: float | None
    state: str


@dataclass(frozen=True)
class PictureRow:
    url: str
    width: int
    height: int
    verdict: str | None  # None: the search that found it has not ended yet
    stage_tag: str | None
    name: str
    border: int  # 0 = a full tiling
    symmetric: bool = False


@dataclass(frozen=True)
class StripResultRow:
    width: int
    rows: int
    verdict: str
    duration_s: float
    name: str
    submitted: float
    source: str


@dataclass(frozen=True)
class StripFillRow:
    fill_id: int
    width: int
    rows: int
    name: str
    submitted: float


@dataclass(frozen=True)
class PeriodicResultRow:
    result_id: int
    width: int
    start: int
    period: int
    verdict: str  # PERIODIC_FOUND, PERIODIC_NONE or PERIODIC_TIMED_OUT
    name: str
    submitted: float


@dataclass(frozen=True)
class ExactResultRow:
    result_id: int
    width: int
    outcome: str
    row: int
    repeat_from: int
    first_row: int
    counts: tuple[int, ...]
    compute_s: float
    lookahead: int
    name: str
    submitted: float


@dataclass(frozen=True)
class ExactProgressRow:
    width: int
    name: str
    job_id: str
    row: int
    states: int
    resumable: bool
    updated: float


PERIODIC_FOUND = "found"
PERIODIC_NONE = "none"
PERIODIC_TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class ResultRow:
    width: int
    height: int
    verdict: str
    stage_tag: str
    duration_s: float
    name: str
    ip: str | None
    threads: int | None
    peak_ram_mb: int | None
    est_ram_mb: int | None
    submitted: float
    flagged: bool
    hard: bool
    source: str


class Database:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(results)")}
        for column, kind in (("picture_border", "INTEGER"), ("picture", "TEXT")):
            if column not in columns:  # databases created before pictures existed
                self._conn.execute(f"ALTER TABLE results ADD COLUMN {column} {kind}")
        frame_columns = {r[1] for r in self._conn.execute("PRAGMA table_info(frames)")}
        if "symmetric" not in frame_columns:  # created by version 0.8
            self._conn.execute(
                "ALTER TABLE frames ADD COLUMN symmetric INTEGER NOT NULL DEFAULT 0"
            )
        progress_columns = {
            r[1] for r in self._conn.execute("PRAGMA table_info(exact_progress)")
        }
        result_columns = {
            r[1] for r in self._conn.execute("PRAGMA table_info(exact_results)")
        }
        if "lookahead" not in result_columns:  # created by version 0.13.0 or 0.13.1
            self._conn.execute(
                "ALTER TABLE exact_results ADD COLUMN lookahead INTEGER NOT NULL DEFAULT 0"
            )
        if (
            "resumable" not in progress_columns
        ):  # created by version 0.13.0, whose apps always resume
            self._conn.execute(
                "ALTER TABLE exact_progress ADD COLUMN resumable INTEGER NOT NULL DEFAULT 1"
            )
        # runs given back for good before 0.14.0's server dropped their progress on release
        self._conn.execute(
            "DELETE FROM exact_progress WHERE job_id IN (SELECT job_id FROM jobs WHERE state = 'released'"
            " AND end_reason != 'closed')"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # jobs
    def add_job(
        self,
        job_id: str,
        nonce: str,
        width: int,
        height: int,
        name: str,
        ip: str,
        lane: str,
        stages: list[dict],
        threads: int,
        est_ram_mb: int,
        client_version: str,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (job_id, nonce, width, height, name, ip, lane, stages, threads, est_ram_mb,"
                " client_version, created, heartbeat, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')",
                (
                    job_id,
                    nonce,
                    width,
                    height,
                    name,
                    ip,
                    lane,
                    json.dumps(stages),
                    threads,
                    est_ram_mb,
                    client_version,
                    now,
                    now,
                ),
            )

    def job(self, job_id: str) -> JobRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, nonce, width, height, name, ip, lane, stages, threads, est_ram_mb, created,"
                " heartbeat, client_state, idle_since, state FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return JobRow(*row[:7], json.loads(row[7]), *row[8:])

    def active_jobs(self) -> list[JobRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, nonce, width, height, name, ip, lane, stages, threads, est_ram_mb, created,"
                " heartbeat, client_state, idle_since, state FROM jobs WHERE state = 'active'"
            ).fetchall()
        return [JobRow(*r[:7], json.loads(r[7]), *r[8:]) for r in rows]

    def touch_job(self, job_id: str, client_state: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET heartbeat = ?, client_state = ?, idle_since = CASE WHEN ? = 'running' THEN NULL"
                " ELSE COALESCE(idle_since, ?) END WHERE job_id = ?",
                (now, client_state, client_state, now, job_id),
            )

    def end_job(
        self, job_id: str, state: str, reason: str, peak_ram_mb: int | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET state = ?, end_reason = ?, peak_ram_mb = ? WHERE job_id = ?",
                (state, reason, peak_ram_mb, job_id),
            )

    def memory_failures(self) -> dict[tuple[int, int], int]:
        """Largest RAM cap (est) a size has failed with."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT width, height, MAX(est_ram_mb) FROM jobs WHERE end_reason = 'memory'"
                " GROUP BY width, height"
            ).fetchall()
        return {(w, h): m for w, h, m in rows}

    # results
    def add_result(
        self,
        *,
        job_id: str | None,
        width: int,
        height: int,
        verdict: str,
        stage_tag: str,
        duration_s: float,
        name: str,
        ip: str | None,
        threads: int | None,
        peak_ram_mb: int | None,
        est_ram_mb: int | None,
        client_version: str | None,
        flagged: bool,
        flag_reason: str,
        hard: bool,
        source: str,
        solution: list | None,
        submitted: float | None = None,
        picture_border: int | None = None,
        picture: list | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO results (job_id, width, height, verdict, stage_tag, duration_s, name, ip, threads,"
                " peak_ram_mb, est_ram_mb, client_version, submitted, flagged, flag_reason, hard, source, solution,"
                " picture_border, picture) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    width,
                    height,
                    verdict,
                    stage_tag,
                    duration_s,
                    name,
                    ip,
                    threads,
                    peak_ram_mb,
                    est_ram_mb,
                    client_version,
                    submitted or time.time(),
                    int(flagged),
                    flag_reason,
                    int(hard),
                    source,
                    json.dumps(solution) if solution is not None else None,
                    picture_border,
                    json.dumps(picture) if picture is not None else None,
                ),
            )

    def recent_pictures(self, limit: int) -> list[PictureRow]:
        """Newest first: tilings and old apps' borders (sent with the result), and borders saved on the way
        (with the outcome of their search, once it ended)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ("
                " SELECT 'picture', id, submitted, width, height, verdict, stage_tag, name, picture_border,"
                f" {HUNT_ROW} FROM results WHERE picture IS NOT NULL AND flagged = 0"
                " UNION ALL"
                " SELECT 'frame', f.id, f.submitted, f.width, f.height, r.verdict, r.stage_tag, f.name, f.border,"
                " f.symmetric FROM frames f LEFT JOIN results r ON r.job_id = f.job_id AND r.flagged = 0"
                ") ORDER BY 3 DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [PictureRow(f"/{r[0]}/{r[1]}.png", *r[3:9], bool(r[9])) for r in rows]

    def picture(self, result_id: int) -> tuple[int, int, list, bool] | None:
        """(width, height, placements, from the symmetric hunt)"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT width, height, picture, {HUNT_ROW} FROM results WHERE id = ?"
                " AND picture IS NOT NULL",
                (result_id,),
            ).fetchone()
        return (row[0], row[1], json.loads(row[2]), bool(row[3])) if row else None

    def add_frame(
        self,
        job_id: str,
        width: int,
        height: int,
        border: int,
        name: str,
        placements: list,
        symmetric: bool = False,
    ) -> bool:
        """Keep the border filling unless the job already saved one at least as deep; True if kept."""
        with self._lock:
            return (
                self._conn.execute(
                    "INSERT INTO frames (job_id, width, height, border, name, submitted, placements,"
                    " symmetric) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET"
                    " border = excluded.border,"
                    " submitted = excluded.submitted, placements = excluded.placements"
                    " WHERE excluded.border > frames.border",
                    (
                        job_id,
                        width,
                        height,
                        border,
                        name,
                        time.time(),
                        json.dumps(placements),
                        int(symmetric),
                    ),
                ).rowcount
                > 0
            )

    def frame(self, frame_id: int) -> tuple[int, int, int, list, bool] | None:
        """(border, width, height, placements, symmetric)"""
        with self._lock:
            row = self._conn.execute(
                "SELECT border, width, height, placements, symmetric FROM frames WHERE id = ?",
                (frame_id,),
            ).fetchone()
        if row is None:
            return None
        return row[0], row[1], row[2], json.loads(row[3]), bool(row[4])

    def filled_border(self, width: int, height: int, symmetric: bool = False) -> int:
        """Deepest border anyone has filled for this size (either orientation), 0 if none. A symmetric
        filling is an ordinary one too; with symmetric=True only symmetric fillings count."""
        frames_where = "symmetric = 1 AND " if symmetric else ""
        results_where = f"{HUNT_ROW} AND " if symmetric else ""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(b) FROM ("
                f" SELECT border AS b FROM frames WHERE {frames_where}"
                " ((width = ? AND height = ?) OR (width = ? AND height = ?))"
                " UNION ALL"
                " SELECT picture_border FROM results WHERE picture_border > 0 AND flagged = 0"
                f" AND {results_where}((width = ? AND height = ?) OR (width = ? AND height = ?)))",
                (width, height, height, width) * 2,
            ).fetchone()
        return row[0] or 0

    def results(self, kind: str = "main") -> list[ResultRow]:
        """kind: "main" (what settles sizes), "hunt" (the symmetric hunt) or "all"."""
        where = RESULT_KINDS[kind]
        with self._lock:
            rows = self._conn.execute(
                "SELECT width, height, verdict, stage_tag, duration_s, name, ip, threads, peak_ram_mb, est_ram_mb,"
                f" submitted, flagged, hard, source FROM results WHERE {where} ORDER BY id"
            ).fetchall()
        return [ResultRow(*r[:11], bool(r[11]), bool(r[12]), r[13]) for r in rows]

    def solution(self, width: int, height: int) -> list | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT solution FROM results WHERE width = ? AND height = ? AND verdict = 'possible'"
                " AND flagged = 0 ORDER BY id LIMIT 1",
                (width, height),
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def retag_import(
        self, width: int, height: int, verdict: str, duration_s: int, stage_tag: str
    ) -> int:
        with self._lock:
            return self._conn.execute(
                "UPDATE results SET stage_tag = ? WHERE source = 'import' AND stage_tag = 'external'"
                " AND width = ? AND height = ? AND verdict = ? AND CAST(ROUND(duration_s) AS INTEGER) = ?",
                (stage_tag, width, height, verdict, duration_s),
            ).rowcount

    # half-strip depth
    def add_strip_result(
        self,
        *,
        job_id: str | None,
        width: int,
        rows: int,
        verdict: str,
        duration_s: float,
        name: str,
        ip: str | None,
        threads: int | None,
        peak_ram_mb: int | None,
        client_version: str | None,
        source: str,
        submitted: float | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO strip_results (job_id, width, rows, verdict, duration_s, name, ip, threads,"
                " peak_ram_mb, client_version, submitted, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    width,
                    rows,
                    verdict,
                    duration_s,
                    name,
                    ip,
                    threads,
                    peak_ram_mb,
                    client_version,
                    submitted or time.time(),
                    source,
                ),
            )

    def import_strip_result(
        self, width: int, rows: int, verdict: str, duration_s: float, name: str
    ) -> bool:
        """Add a known result unless the same one was imported before; True if added."""
        with self._lock:
            return (
                self._conn.execute(
                    "INSERT INTO strip_results (width, rows, verdict, duration_s, name, submitted, source)"
                    " SELECT ?,?,?,?,?,?, 'import' WHERE NOT EXISTS (SELECT 1 FROM strip_results"
                    " WHERE source = 'import' AND width = ? AND rows = ? AND verdict = ?)",
                    (
                        width,
                        rows,
                        verdict,
                        duration_s,
                        name,
                        time.time(),
                        width,
                        rows,
                        verdict,
                    ),
                ).rowcount
                > 0
            )

    def strip_results(self) -> list[StripResultRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT width, rows, verdict, duration_s, name, submitted, source FROM strip_results"
                " ORDER BY id"
            ).fetchall()
        return [StripResultRow(*r) for r in rows]

    def add_strip_fill(
        self, job_id: str, width: int, rows: int, name: str, placements: list
    ) -> bool:
        """Keep the filling unless the job already saved one at least as deep; True if kept."""
        with self._lock:
            return (
                self._conn.execute(
                    "INSERT INTO strip_fills (job_id, width, rows, name, submitted, placements)"
                    " VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET rows = excluded.rows,"
                    " submitted = excluded.submitted, placements = excluded.placements"
                    " WHERE excluded.rows > strip_fills.rows",
                    (job_id, width, rows, name, time.time(), json.dumps(placements)),
                ).rowcount
                > 0
            )

    def strip_fills(self) -> list[StripFillRow]:
        """Every saved filling, oldest first (without the placements)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, width, rows, name, submitted FROM strip_fills ORDER BY id"
            ).fetchall()
        return [StripFillRow(*r) for r in rows]

    def strip_fill(self, fill_id: int) -> tuple[int, int, list] | None:
        """(width, rows, placements)"""
        with self._lock:
            row = self._conn.execute(
                "SELECT width, rows, placements FROM strip_fills WHERE id = ?",
                (fill_id,),
            ).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    # fill-forever certificates
    def add_periodic_result(
        self,
        *,
        job_id: str,
        width: int,
        start: int,
        period: int,
        verdict: str,
        duration_s: float,
        name: str,
        ip: str | None,
        client_version: str | None,
        placements: list | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO periodic_results (job_id, width, start, period, verdict, duration_s, name, ip,"
                " client_version, submitted, source, placements) VALUES (?,?,?,?,?,?,?,?,?,?,'volunteer',?)",
                (
                    job_id,
                    width,
                    start,
                    period,
                    verdict,
                    duration_s,
                    name,
                    ip,
                    client_version,
                    time.time(),
                    None if placements is None else json.dumps(placements),
                ),
            )

    def import_periodic_certificate(
        self,
        width: int,
        start: int,
        period: int,
        duration_s: float,
        name: str,
        placements: list,
    ) -> bool:
        """Add a known certificate unless one for this width was imported before; True if added."""
        with self._lock:
            return (
                self._conn.execute(
                    "INSERT INTO periodic_results (width, start, period, verdict, duration_s, name, submitted,"
                    " source, placements) SELECT ?,?,?,?,?,?,?, 'import', ? WHERE NOT EXISTS (SELECT 1"
                    " FROM periodic_results WHERE source = 'import' AND width = ?)",
                    (
                        width,
                        start,
                        period,
                        PERIODIC_FOUND,
                        duration_s,
                        name,
                        time.time(),
                        json.dumps(placements),
                        width,
                    ),
                ).rowcount
                > 0
            )

    def periodic_results(self) -> list[PeriodicResultRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, width, start, period, verdict, name, submitted FROM periodic_results ORDER BY id"
            ).fetchall()
        return [PeriodicResultRow(*r) for r in rows]

    def periodic_certificate(self, result_id: int) -> tuple[int, int, int, list] | None:
        """(width, start, period, placements) of a found repeating filling."""
        with self._lock:
            row = self._conn.execute(
                "SELECT width, start, period, placements FROM periodic_results"
                " WHERE id = ? AND placements IS NOT NULL",
                (result_id,),
            ).fetchone()
        return (row[0], row[1], row[2], json.loads(row[3])) if row else None

    def add_exact_result(
        self,
        *,
        job_id: str,
        width: int,
        outcome: str,
        row: int,
        repeat_from: int,
        first_row: int,
        counts: tuple[int, ...],
        compute_s: float,
        name: str,
        ip: str | None,
        peak_ram_mb: int | None,
        client_version: str | None,
        lookahead: int = 0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO exact_results (job_id, width, outcome, row, repeat_from, first_row, counts,"
                " compute_s, lookahead, name, ip, peak_ram_mb, client_version, submitted)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    width,
                    outcome,
                    row,
                    repeat_from,
                    first_row,
                    json.dumps(list(counts)),
                    compute_s,
                    lookahead,
                    name,
                    ip,
                    peak_ram_mb,
                    client_version,
                    time.time(),
                ),
            )
            self._conn.execute("DELETE FROM exact_progress WHERE width = ?", (width,))

    def exact_results(self) -> list[ExactResultRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, width, outcome, row, repeat_from, first_row, counts, compute_s, lookahead, name,"
                " submitted FROM exact_results ORDER BY id"
            ).fetchall()
        return [ExactResultRow(*r[:6], tuple(json.loads(r[6])), *r[7:]) for r in rows]

    def set_exact_progress(
        self,
        width: int,
        name: str,
        job_id: str,
        row: int,
        states: int,
        resumable: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO exact_progress (width, name, job_id, row, states, resumable, updated)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(width) DO UPDATE SET name = excluded.name,"
                " job_id = excluded.job_id, row = excluded.row, states = excluded.states,"
                " resumable = excluded.resumable, updated = excluded.updated",
                (width, name, job_id, row, states, int(resumable), time.time()),
            )

    def drop_exact_progress(self, width: int, job_id: str, name: str) -> None:
        """Forget a run's progress (it was given back), unless someone else has reported on the width since.
        Matching the name too covers a resumed run given back before it reported a row: the progress is
        still under the earlier job's id."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM exact_progress WHERE width = ? AND (job_id = ? OR name = ?)",
                (width, job_id, name),
            )

    def exact_progress(self) -> dict[int, ExactProgressRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT width, name, job_id, row, states, resumable, updated FROM exact_progress"
            ).fetchall()
        return {r[0]: ExactProgressRow(*r[:5], bool(r[5]), r[6]) for r in rows}

    def use_salt(self, salt: str) -> bool:
        """Record a salt; False if it was used before."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO used_salts (salt, used) VALUES (?, ?)",
                    (salt, time.time()),
                )
            except sqlite3.IntegrityError:
                return False
        return True
