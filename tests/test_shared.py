import pytest

from tilingvol.client.model import build_model
from tilingvol.shared import token
from tilingvol.shared.pieces import GUN, SIBLING, all_orientations
from tilingvol.shared.verify import TilingError, verify_tiling


def research_admissible(w: int, h: int) -> bool:
    """The rule used by the research sweep (rect_sweep.py)."""
    if (w * h) % 20:
        return False
    if w % 2 and h % 8:
        return False
    return not (h % 2 and w % 8)


def test_both_pieces_have_eight_orientations():
    assert len(GUN.orientations) == 8
    assert len(SIBLING.orientations) == 8
    assert all(len(shape) == 10 for shape in GUN.orientations)


def test_orientation_order_is_canonical():
    assert all_orientations(GUN.cells) == all_orientations(list(reversed(GUN.cells)))


def test_gun_admissibility_matches_research_rule():
    for w in range(1, 120):
        for h in range(1, 120):
            assert GUN.admissible(w, h) == research_admissible(w, h), (w, h)


def test_gun_candidates_skip_exactly_dead_sides():
    sizes = GUN.candidate_sizes()
    assert (135, 184) in sizes
    assert all(136 not in s and min(s) >= 135 for s in sizes)


def solve_sibling(width: int, height: int) -> list[list[int]]:
    from ortools.sat.python import cp_model

    built = build_model(SIBLING, width, height, 0, False)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 4
    solver.parameters.max_time_in_seconds = 60
    assert solver.Solve(built.model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    return [
        list(built.placement_of[c])
        for c, v in built.variables.items()
        if solver.Value(v)
    ]


@pytest.fixture(scope="module")
def sibling_tiling() -> list[list[int]]:
    return solve_sibling(19, 28)


def test_verifier_accepts_a_real_tiling(sibling_tiling):
    verify_tiling(SIBLING, 19, 28, sibling_tiling)


@pytest.mark.parametrize(
    "mutation", ["drop", "shift", "orientation", "swap_size", "garbage"]
)
def test_verifier_rejects_broken_tilings(sibling_tiling, mutation):
    pieces = [list(p) for p in sibling_tiling]
    width, height = 19, 28
    if mutation == "drop":
        pieces.pop()
    elif mutation == "shift":
        pieces[0][1] += 1
    elif mutation == "orientation":
        pieces[0][0] = (pieces[0][0] + 1) % 8
    elif mutation == "swap_size":
        width, height = 28, 19
    else:
        pieces[0] = [99, 0, 0]
    with pytest.raises(TilingError):
        verify_tiling(SIBLING, width, height, pieces)


def test_token_round_trip_and_tamper():
    key = b"k" * 32
    salt = token.new_salt()
    payload = token.signing_payload(
        "j", "n", "alice", 150, 170, "impossible", 1, 1234.5, salt, None
    )
    signature = token.sign(key, payload)
    assert token.check(key, payload, signature)
    tampered = token.signing_payload(
        "j", "n", "alice", 150, 172, "impossible", 1, 1234.5, salt, None
    )
    assert not token.check(key, tampered, signature)
    assert not token.check(b"x" * 32, payload, signature)


def test_parallel_settings_split_threads_and_ram(monkeypatch):
    import os

    from tilingvol.client import settings as settings_module
    from tilingvol.client.settings import Settings

    monkeypatch.setattr(settings_module, "installed_ram_mb", lambda: 65536)
    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    base = Settings(
        name="x", max_ram_mb=32768, max_threads=20, server_url="http://h", parallel=2
    )
    slot = base.validated().slot_settings()
    assert (slot.max_ram_mb, slot.max_threads, slot.parallel) == (16384, 10, 1)
    with pytest.raises(ValueError):
        Settings(
            name="x", max_ram_mb=32768, max_threads=4, server_url="http://h", parallel=5
        ).validated()
    with pytest.raises(ValueError):
        Settings(
            name="x", max_ram_mb=1536, max_threads=8, server_url="http://h", parallel=2
        ).validated()
