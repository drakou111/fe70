"""A glitched memory reading must neither kill a search nor make the server refuse the release."""

from types import SimpleNamespace

import psutil

from tilingvol.client import memory_guard
from tilingvol.client.api import ApiClient
from tilingvol.shared.protocol import (
    MAX_PEAK_RAM_MB,
    RELEASE_MEMORY,
    Job,
    ResultSubmission,
    Stage,
)

GLITCH_BYTES = (
    117449263 * memory_guard.MB
)  # what a volunteer's Windows machine reported


class FakeProcess:
    def __init__(self, private: int, rss: int) -> None:
        self.info = SimpleNamespace(private=private, rss=rss)

    def memory_info(self) -> SimpleNamespace:
        return self.info


def test_impossible_private_reading_falls_back_to_resident():
    process = FakeProcess(GLITCH_BYTES, 500 * memory_guard.MB)
    assert memory_guard.process_memory_mb(process) == 500


def test_impossible_reading_never_kills():
    verdict = memory_guard.check(FakeProcess(GLITCH_BYTES, GLITCH_BYTES), cap_mb=9216)
    assert verdict.used_mb is None
    assert not verdict.kill or "free" in verdict.reason


def test_real_reading_over_the_cap_kills():
    verdict = memory_guard.check(FakeProcess(9000 * memory_guard.MB, 0), cap_mb=9216)
    assert verdict.kill and verdict.used_mb == 9000


def test_real_process_reading_is_believable():
    used = memory_guard.process_memory_mb(psutil.Process())
    assert used is not None and 0 < used <= memory_guard.possible_memory_mb()


def test_submission_clamps_peak_ram():
    submission = ResultSubmission(
        "0" * 32, "1" * 32, "n", "impossible", 0, 1.0, 117449263, "s", "t", "0"
    )
    assert submission.to_dict()["peak_ram_mb"] == MAX_PEAK_RAM_MB


def test_release_clamps_peak_ram(monkeypatch):
    sent = []
    client = ApiClient("http://127.0.0.1:1")
    monkeypatch.setattr(client, "_post", lambda path, body: sent.append(body))
    job = Job(
        "0" * 32, "1" * 32, "gun", 200, 220, (Stage(0, False, 600.0),), 1, 0, "t", 60.0
    )
    client.release(job, RELEASE_MEMORY, 117449263)
    assert sent[0]["peak_ram_mb"] == MAX_PEAK_RAM_MB
