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
    assert (138, 140) in sizes
    assert all(min(s) >= 137 for s in sizes)


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


def test_tree_progress_lines_are_parsed_and_throttled():
    from tilingvol.client.solver_process import MSG_PROGRESS, TreeProgress

    sent = []

    class Sink:
        def put(self, item):
            sent.append(item)

    progress = TreeProgress(1, Sink())
    progress.on_line("#Tree   22.28s closed:1/3 unassigned:0 restarts:5")
    progress.on_line(
        "#Tree   22.36s closed:1/13 unassigned:6 restarts:6"
    )  # within 5 s: dropped
    progress.on_line("#Bound  23.00s best:inf next:[0,0]")
    assert sent == [(MSG_PROGRESS, 1, 1, 3)]


def test_tree_workers_need_enough_threads():
    from tilingvol.client.controller import tree_workers_for

    assert [tree_workers_for(t) for t in (1, 2, 3, 4, 8, 10)] == [0, 0, 0, 2, 4, 5]


def test_wall_lemma_shortcut_drops_exactly_the_lemma1_placements():
    from tilingvol.client.model import violates_lemma1, wall_frames

    width, height = 135, 140
    frames = wall_frames(width, height)
    expected = set()
    for orientation, shape in enumerate(GUN.orientations):
        max_x = max(x for x, _ in shape)
        max_y = max(y for _, y in shape)
        for y0 in range(height - max_y):
            for x0 in range(width - max_x):
                cells = [(x0 + dx, y0 + dy) for dx, dy in shape]
                if not any(violates_lemma1(cells, f) for f in frames):
                    expected.add((orientation, x0, y0))
    built = build_model(GUN, width, height, 0, False)
    assert set(built.placement_of.values()) == expected


def frame_status(width: int, height: int, border: int, lookahead: bool) -> str:
    from ortools.sat.python import cp_model

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 4
    solver.parameters.max_time_in_seconds = 60
    built = build_model(SIBLING, width, height, border, lookahead)
    return solver.StatusName(solver.Solve(built.model))


def test_lookahead_keeps_its_strength():
    # 12 x 14: its depth-3 border can be filled, but every filling leaves an interior cell no free piece fits
    assert frame_status(12, 14, 3, lookahead=False) == "OPTIMAL"
    assert frame_status(12, 14, 3, lookahead=True) == "INFEASIBLE"
    assert frame_status(19, 28, 3, lookahead=True) == "OPTIMAL"  # it really tiles


def test_lookahead_accepts_the_border_of_a_real_tiling(sibling_tiling):
    from ortools.sat.python import cp_model

    border = 3
    built = build_model(SIBLING, 19, 28, border, True)
    by_placement = {
        p: v for v, p in zip(built.variables.values(), built.placement_of.values())
    }
    kept = [tuple(p) for p in sibling_tiling if tuple(p) in by_placement]
    assert kept
    for placement in kept:
        built.model.Add(by_placement[placement] == 1)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 4
    assert solver.Solve(built.model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def test_empty_form_fields_mean_the_defaults(monkeypatch):
    import os

    from tilingvol.client import settings as settings_module
    from tilingvol.client.settings import Form, form_from_saved

    monkeypatch.setattr(settings_module, "installed_ram_mb", lambda: 65536)
    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    resolved = Form(name="x").resolve()
    s = resolved.settings
    assert (s.max_ram_mb, s.max_threads, s.parallel) == (32768, 23, 1)
    assert (s.time_limit_s, s.no_time_limit, s.custom_stages) == (None, False, "")
    assert (resolved.requested_size, resolved.requested_widths) == (None, ())
    assert s.server_url == settings_module.default_server_url()

    widths = Form(name="x", request="149, 150", time_limit_hours="5").resolve()
    assert widths.requested_widths == (149, 150)
    assert widths.settings.time_limit_s == 5 * 3600
    assert Form(name="x", time_limit_hours="0").resolve().settings.no_time_limit
    board = Form(name="x", search="board", request="150x168").resolve()
    assert board.requested_size == (150, 168)
    for bad in (
        Form(name="x", ram_gb="lots"),
        Form(name="x", threads="2.5"),
        Form(name="x", search="board", request="150"),
        Form(name="x", time_limit_hours="-1"),
    ):
        with pytest.raises(ValueError):
            bad.resolve()

    # a settings file from 0.10: default numbers become empty fields
    old = form_from_saved(
        {
            "name": "x",
            "max_ram_mb": 32768,
            "max_threads": 20,
            "parallel": 2,
            "no_time_limit": True,
            "symmetric_hunt": True,
            "custom_stages": "24",
        }
    )
    assert (old.ram_gb, old.threads, old.parallel, old.time_limit_hours) == (
        "",
        "20",
        "2",
        "0",
    )
    assert (old.search, old.stages) == ("symmetric", "24")
