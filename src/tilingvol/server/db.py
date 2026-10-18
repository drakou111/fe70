"""SQLite storage. One connection guarded by a lock (the API runs in a single process); every query is
parameterized."""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

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
    placements TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS frames_size ON frames(width, height);
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
                " SELECT 'picture', id, submitted, width, height, verdict, stage_tag, name, picture_border"
                " FROM results WHERE picture IS NOT NULL AND flagged = 0"
                " UNION ALL"
                " SELECT 'frame', f.id, f.submitted, f.width, f.height, r.verdict, r.stage_tag, f.name, f.border"
                " FROM frames f LEFT JOIN results r ON r.job_id = f.job_id AND r.flagged = 0"
                ") ORDER BY 3 DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [PictureRow(f"/{r[0]}/{r[1]}.png", *r[3:]) for r in rows]

    def picture(self, result_id: int) -> tuple[int, int, list] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT width, height, picture FROM results WHERE id = ? AND picture IS NOT NULL",
                (result_id,),
            ).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    def add_frame(
        self,
        job_id: str,
        width: int,
        height: int,
        border: int,
        name: str,
        placements: list,
    ) -> bool:
        """Keep the border filling unless the job already saved one at least as deep; True if kept."""
        with self._lock:
            return (
                self._conn.execute(
                    "INSERT INTO frames (job_id, width, height, border, name, submitted, placements)"
                    " VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET border = excluded.border,"
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
                    ),
                ).rowcount
                > 0
            )

    def frame(self, frame_id: int) -> tuple[int, int, int, list] | None:
        """(border, width, height, placements)"""
        with self._lock:
            row = self._conn.execute(
                "SELECT border, width, height, placements FROM frames WHERE id = ?",
                (frame_id,),
            ).fetchone()
        return (row[0], row[1], row[2], json.loads(row[3])) if row else None

    def filled_border(self, width: int, height: int) -> int:
        """Deepest border anyone has filled for this size (either orientation), 0 if none."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(b) FROM ("
                " SELECT border AS b FROM frames WHERE (width = ? AND height = ?) OR (width = ? AND height = ?)"
                " UNION ALL"
                " SELECT picture_border FROM results WHERE picture_border > 0 AND flagged = 0"
                " AND ((width = ? AND height = ?) OR (width = ? AND height = ?)))",
                (width, height, height, width) * 2,
            ).fetchone()
        return row[0] or 0

    def results(self) -> list[ResultRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT width, height, verdict, stage_tag, duration_s, name, ip, threads, peak_ram_mb, est_ram_mb,"
                " submitted, flagged, hard, source FROM results ORDER BY id"
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
