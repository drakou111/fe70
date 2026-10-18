"""Seed the database with the research sweep's results (output/rect_sweep.txt of the tiling repo).
Run: TILINGVOL_IMPORT_FILE=rect_sweep.txt uv run python -m tilingvol.server.import_sweep
Safe to run again on a longer file: only lines not imported before are added; nothing is overwritten."""

import os
import re
from pathlib import Path

from tilingvol.server.config import ServerConfig
from tilingvol.server.db import Database
from tilingvol.shared.protocol import (
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
)

SWEEP_NAME = "Drakou111"
EXTERNAL_NAME = "zach"
LINE = re.compile(r"^(\d+)x(\d+) (SAT|UNSAT|UNKNOWN) (\d+)s ?(.*)$")
EXTERNAL_DEPTH = re.compile(
    r"frame (\d+)"
)  # the collaborator's border depth (with extendability)
VERDICT = {
    "SAT": VERDICT_POSSIBLE,
    "UNSAT": VERDICT_IMPOSSIBLE,
    "UNKNOWN": VERDICT_TIMED_OUT,
}


def parse(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LINE.match(line.strip())
        if not match:
            continue
        w, h, verdict, seconds, rest = (
            int(match[1]),
            int(match[2]),
            match[3],
            int(match[4]),
            match[5],
        )
        if verdict == "SAT":
            raise ValueError(
                f"the sweep file claims a tiling ({line!r}); add it by hand with its solution"
            )
        external = "external" in rest
        if external:
            depth = EXTERNAL_DEPTH.search(rest)
            tag = f"frame{depth[1]}L" if depth else "external"
        else:
            tag = rest.split()[0] if rest else "full"
        rows.append(
            {
                "width": min(w, h),
                "height": max(w, h),
                "verdict": VERDICT[verdict],
                "stage_tag": tag,
                "duration_s": float(seconds),
                "name": EXTERNAL_NAME if external else SWEEP_NAME,
                "hard": "(hard)" in rest,
            }
        )
    return rows


def main() -> None:
    path = Path(os.environ.get("TILINGVOL_IMPORT_FILE", "rect_sweep.txt"))
    config = ServerConfig.from_env()
    db = Database(config.db_path)
    # every result is one line; one already in the database (same size, verdict, stage, time) is skipped
    # a result is one line; one already imported (same size, verdict and time) is skipped, except that a
    # collaborator row first imported without its border depth gets the depth now
    imported = {
        (r.width, r.height, r.verdict, round(r.duration_s)): r.stage_tag
        for r in db.results()
        if r.source == "import"
    }
    rows, retagged = [], 0
    for row in parse(path):
        key = (row["width"], row["height"], row["verdict"], round(row["duration_s"]))
        if key not in imported:
            rows.append(row)
        elif imported[key] == "external" and row["stage_tag"] != "external":
            retagged += db.retag_import(*key, row["stage_tag"])
    for row in rows:
        db.add_result(
            job_id=None,
            ip=None,
            threads=None,
            peak_ram_mb=None,
            est_ram_mb=None,
            client_version=None,
            flagged=False,
            flag_reason="",
            source="import",
            solution=None,
            **row,
        )
    print(
        f"imported {len(rows)} new results from {path}; added border depths to {retagged} older rows"
    )


if __name__ == "__main__":
    main()
