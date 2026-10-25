"""Copy the Pi's newest hourly database backup to this PC (output/backups), so a dead SD card does not
take the backups with it. Only data travels: the file is checked with a read-only SQLite open and never
run. Keeps the newest KEEP_LOCAL copies here.
Needs TILINGVOL_PI_SSH in .env. Run: uv run python scripts/pull_backup.py
Hourly: see "Backups" in deploy/PI_SETUP.md for the Task Scheduler line."""

import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish import ROOT, run, settings

from tilingvol.server.backup import NAME

REMOTE_DIR = "/var/backups/tilingvol"
LOCAL_DIR = ROOT / "output" / "backups"
KEEP_LOCAL = 72
LOG_FILE = LOCAL_DIR / "pull.log"

log = logging.getLogger("pull_backup")


def newest_remote(remote: str) -> str:
    listing = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", remote, f"ls -1 {REMOTE_DIR}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    # names not matching the backup pattern are ignored, whatever the Pi lists
    names = sorted(n for n in listing.stdout.split() if NAME.fullmatch(n))
    if not names:
        raise SystemExit(f"no backups in {REMOTE_DIR} on the Pi yet")
    return names[-1]


def check_copy(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        raise SystemExit(f"{path.name} failed its integrity check: {result}")


def main() -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    remote, _, _ = settings()
    name = newest_remote(remote)
    target = LOCAL_DIR / name
    if target.exists():
        log.info("already have %s", name)
        return
    partial = target.with_suffix(".partial")
    run(["scp", "-B", "-q", f"{remote}:{REMOTE_DIR}/{name}", str(partial)])
    check_copy(partial)
    partial.replace(target)
    log.info("pulled %s (%d bytes)", name, target.stat().st_size)
    copies = sorted(p for p in LOCAL_DIR.iterdir() if NAME.fullmatch(p.name))
    for old in copies[:-KEEP_LOCAL]:
        old.unlink()
        log.info("removed old copy %s", old.name)


if __name__ == "__main__":
    try:
        main()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        SystemExit,
    ) as error:
        log.error("pull failed: %s", error)
        raise
