"""CP-SAT rectangle model, ported from the research repo's rect_strong.py.

border > 0 builds the frame relaxation: cells within `border` of the edge are covered exactly once, the
interior at most once, and placements wholly inside the interior are dropped. With lookahead, every
uncovered interior cell must keep a placement that overlaps no chosen piece. If the relaxation is
infeasible, so is the rectangle.

For the gun the proven wall lemmas are added (research PROOF_NOTES.md section 1): Lemma 1 (a piece
perpendicular to a wall and touching it has its bump at distance 1..3), Lemma 2 (floor neighbour) and
Lemma 3 (forced corner). They are stated once in wall coordinates (u along the wall, v away from it)."""

from dataclasses import dataclass

from ortools.sat.python import cp_model

from tilingvol.shared.pieces import PieceSpec

GUN_BAR_LENGTH = 7
WALLS = ("bottom", "top", "left", "right")


@dataclass(frozen=True)
class WallFrame:
    """One wall of the rectangle seen from inside, with a direction along it."""

    wall: str
    flip: bool
    width: int
    height: int

    def to_xy(self, u: int, v: int) -> tuple[int, int]:
        w, h = self.width, self.height
        if self.wall in ("bottom", "top"):
            x = w - 1 - u if self.flip else u
            return (x, v) if self.wall == "bottom" else (x, h - 1 - v)
        y = h - 1 - u if self.flip else u
        return (v, y) if self.wall == "left" else (w - 1 - v, y)

    def to_uv(self, x: int, y: int) -> tuple[int, int]:
        w, h = self.width, self.height
        if self.wall in ("bottom", "top"):
            u = w - 1 - x if self.flip else x
            return u, (y if self.wall == "bottom" else h - 1 - y)
        u = h - 1 - y if self.flip else y
        return u, (x if self.wall == "left" else w - 1 - x)


@dataclass
class BuiltModel:
    model: cp_model.CpModel
    variables: dict[frozenset, cp_model.IntVar]
    placement_of: dict[frozenset, tuple[int, int, int]]  # cells -> (orientation, x, y)


def wall_frames(width: int, height: int) -> list[WallFrame]:
    return [WallFrame(w, f, width, height) for w in WALLS for f in (False, True)]


def split_bar_bump(cells_uv: list[tuple[int, int]]) -> tuple[str, list, list]:
    """(axis, bar, bump): axis 'v' if the bar runs away from the wall."""
    by_u, by_v = {}, {}
    for u, v in cells_uv:
        by_u.setdefault(u, []).append((u, v))
        by_v.setdefault(v, []).append((u, v))
    for line in by_u.values():
        if len(line) == GUN_BAR_LENGTH:
            return "v", line, [c for c in cells_uv if c not in line]
    for line in by_v.values():
        if len(line) == GUN_BAR_LENGTH:
            return "u", line, [c for c in cells_uv if c not in line]
    raise ValueError("not a gun piece")


def violates_lemma1(cells_xy, frame: WallFrame) -> bool:
    axis, bar, bump = split_bar_bump([frame.to_uv(x, y) for x, y in cells_xy])
    if axis != "v" or min(v for _, v in bar) != 0:
        return False
    return sorted(v for _, v in bump) != [1, 2, 3]


def canonical_corner() -> list[list[tuple[int, int]]]:
    """Lemma 3 in bottom-wall coordinates, corner at u = v = 0; S is given by its bar only."""
    p0 = [(0, v) for v in range(7)] + [(1, v) for v in (1, 2, 3)]
    q0 = [(u, 0) for u in range(1, 8)] + [(u, 1) for u in (2, 3, 4)]
    r = [(u, 1) for u in range(5, 12)] + [(u, 0) for u in (8, 9, 10)]
    s_bar = [(u, 0) for u in range(11, 18)]
    return [p0, q0, r, s_bar]


def corner_options(width: int, height: int) -> list[list[list[frozenset]]]:
    pairs = [
        (("bottom", False), ("left", False)),
        (("bottom", True), ("right", False)),
        (("top", False), ("left", True)),
        (("top", True), ("right", True)),
    ]
    out = []
    for first, second in pairs:
        alternatives = []
        for wall, flip in (first, second):
            frame = WallFrame(wall, flip, width, height)
            alternatives.append(
                [
                    frozenset(frame.to_xy(u, v) for u, v in piece)
                    for piece in canonical_corner()
                ]
            )
        out.append(alternatives)
    return out


def lemma2_pairs(width: int, height: int) -> list[tuple[frozenset, list[frozenset]]]:
    out = []
    for frame in wall_frames(width, height):
        length = width if frame.wall in ("bottom", "top") else height
        depth = height if frame.wall in ("bottom", "top") else width
        if depth < GUN_BAR_LENGTH:
            continue
        for x in range(length - 8):
            trigger = [(x, v) for v in range(7)] + [(x + 1, v) for v in (1, 2, 3)]
            bar = [(u, 0) for u in range(x + 1, x + 8)]
            follows = [
                bar + [(u, 1) for u in (x + 2, x + 3, x + 4)],
                bar + [(u, 1) for u in (x + 4, x + 5, x + 6)],
            ]
            out.append(
                (
                    frozenset(frame.to_xy(*c) for c in trigger),
                    [frozenset(frame.to_xy(*c) for c in f) for f in follows],
                )
            )
    return out


def build_model(
    piece: PieceSpec, width: int, height: int, border: int, lookahead: bool
) -> BuiltModel:
    model = cp_model.CpModel()
    variables: dict[frozenset, cp_model.IntVar] = {}
    placement_of: dict[frozenset, tuple[int, int, int]] = {}
    cover: dict[tuple[int, int], list] = {}
    use_lemmas = piece.use_wall_lemmas
    frames = wall_frames(width, height)
    interior_candidates = []

    def in_frame(x: int, y: int) -> bool:
        return border == 0 or min(x, y, width - 1 - x, height - 1 - y) < border

    for orientation, shape in enumerate(piece.orientations):
        max_x = max(x for x, _ in shape)
        max_y = max(y for _, y in shape)
        for y0 in range(height - max_y):
            for x0 in range(width - max_x):
                cells = frozenset((x0 + dx, y0 + dy) for dx, dy in shape)
                if not any(in_frame(cx, cy) for cx, cy in cells):
                    if lookahead:
                        interior_candidates.append(cells)
                    continue
                if use_lemmas and any(
                    violates_lemma1(cells, frame) for frame in frames
                ):
                    continue
                v = model.NewBoolVar("")
                variables[cells] = v
                placement_of[cells] = (orientation, x0, y0)
                for c in cells:
                    cover.setdefault(c, []).append(v)

    for y in range(height):
        for x in range(width):
            options = cover.get((x, y), [])
            if in_frame(x, y):
                model.AddExactlyOne(options)
            elif options:
                model.AddAtMostOne(options)

    if lookahead and border:
        covered = {}
        for y in range(height):
            for x in range(width):
                options = cover.get((x, y), [])
                is_covered = model.NewBoolVar("")
                if options:
                    model.Add(sum(options) == is_covered)
                else:
                    model.Add(is_covered == 0)
                covered[(x, y)] = is_covered
        free_at: dict[tuple[int, int], list] = {}
        for cells in list(variables) + interior_candidates:
            free = model.NewBoolVar("")
            for c in cells:
                model.AddImplication(free, covered[c].Not())
            for c in cells:
                free_at.setdefault(c, []).append(free)
        for y in range(height):
            for x in range(width):
                if not in_frame(x, y):
                    model.AddBoolOr(free_at.get((x, y), []) + [covered[(x, y)]])

    if use_lemmas:
        for alternatives in corner_options(width, height):
            choice = []
            for pieces in alternatives:
                literal_sets = []
                for need in pieces:
                    if len(need) == piece.area:
                        literal_sets.append(
                            [variables[need]] if need in variables else []
                        )
                    else:
                        literal_sets.append(
                            [v for cells, v in variables.items() if need <= cells]
                        )
                chosen = model.NewBoolVar("")
                for options in literal_sets:
                    if options:
                        model.AddBoolOr(options).OnlyEnforceIf(chosen)
                    else:
                        model.Add(chosen == 0)
                choice.append(chosen)
            model.AddBoolOr(choice)
        for trigger, follows in lemma2_pairs(width, height):
            if trigger in variables:
                model.AddBoolOr(
                    [variables[f] for f in follows if f in variables]
                ).OnlyEnforceIf(variables[trigger])

    return BuiltModel(model, variables, placement_of)
