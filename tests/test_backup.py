"""Hourly backups: a consistent copy while the server writes, and the hourly/daily retention."""

from datetime import UTC, datetime, timedelta

import pytest

from tilingvol.server import backup
from tilingvol.server.db import Database


def test_backup_is_a_working_database_while_the_server_has_it_open(tmp_path):
    db_path = tmp_path / "live.sqlite3"
    live = Database(db_path)
    live.add_strip_result(
        job_id=None,
        width=135,
        rows=48,
        verdict="impossible",
        duration_s=6306.5,
        name="zach",
        ip=None,
        threads=None,
        peak_ram_mb=None,
        client_version=None,
        source="import",
    )
    target = backup.make_backup(
        db_path, tmp_path / "backups", datetime(2026, 10, 25, 14, tzinfo=UTC)
    )
    assert target.name == "tilingvol-20261025-14.sqlite3"
    copy = Database(target)
    assert [(r.width, r.verdict) for r in copy.strip_results()] == [(135, "impossible")]
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_missing_database_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.make_backup(tmp_path / "none.sqlite3", tmp_path, datetime.now(UTC))


def test_prune_keeps_recent_hours_and_one_per_day(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    hours = 60 * 24
    for hour in range(hours):
        (tmp_path / backup.backup_name(start + timedelta(hours=hour))).touch()
    (tmp_path / "notes.txt").touch()
    backup.prune(tmp_path)
    kept = sorted(p.name for p in tmp_path.iterdir() if backup.NAME.fullmatch(p.name))
    newest = start + timedelta(hours=hours - 1)
    hourly = {backup.backup_name(newest - timedelta(hours=h)) for h in range(48)}
    daily = {
        backup.backup_name((newest - timedelta(days=d)).replace(hour=23))
        for d in range(backup.KEEP_DAILY)
    }
    assert set(kept) == hourly | daily
    assert (tmp_path / "notes.txt").exists()
