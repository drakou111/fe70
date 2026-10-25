"""Hourly database snapshot, run by tilingvol-backup.timer on the Pi. SQLite's online backup copies a
consistent database while the server keeps writing; each copy is checked before it replaces anything.
Keeps the newest KEEP_HOURLY copies plus the last copy of each of the newest KEEP_DAILY days.
Run: python -m tilingvol.server.backup (reads TILINGVOL_DB and TILINGVOL_BACKUP_DIR)."""

import logging
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

KEEP_HOURLY = 48
KEEP_DAILY = 30
DEFAULT_BACKUP_DIR = "/var/backups/tilingvol"
NAME = re.compile(r"tilingvol-(\d{8})-(\d{2})\.sqlite3")

log = logging.getLogger(__name__)


def backup_name(moment: datetime) -> str:
    return f"tilingvol-{moment:%Y%m%d-%H}.sqlite3"


def make_backup(db_path: Path, backup_dir: Path, moment: datetime) -> Path:
    if not db_path.is_file():
        raise FileNotFoundError(f"no database at {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / backup_name(moment)
    partial = target.with_suffix(".partial")
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
            check = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copy.close()
    finally:
        source.close()
    if check != "ok":
        partial.unlink()
        raise RuntimeError(f"the backup failed its integrity check: {check}")
    os.replace(partial, target)
    return target


def prune(backup_dir: Path) -> list[Path]:
    """Delete copies outside the hourly and daily windows; returns what was deleted."""
    copies = sorted(
        (p for p in backup_dir.iterdir() if NAME.fullmatch(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    keep = set(copies[:KEEP_HOURLY])
    days_kept: list[str] = []
    for copy in copies:
        day = NAME.fullmatch(copy.name).group(1)
        if day not in days_kept:
            days_kept.append(day)
            if len(days_kept) <= KEEP_DAILY:
                keep.add(copy)
    removed = [p for p in copies if p not in keep]
    for path in removed:
        path.unlink()
    return removed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    db_path = Path(os.environ.get("TILINGVOL_DB", "tilingvol.sqlite3"))
    backup_dir = Path(os.environ.get("TILINGVOL_BACKUP_DIR", DEFAULT_BACKUP_DIR))
    target = make_backup(db_path, backup_dir, datetime.now(UTC))
    log.info("backed up %s to %s (%d bytes)", db_path, target, target.stat().st_size)
    for path in prune(backup_dir):
        log.info("removed old backup %s", path.name)


if __name__ == "__main__":
    main()
