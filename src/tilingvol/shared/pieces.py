"""Piece definitions shared by client and server. Orientation order is canonical (sorted), so a placement
(orientation, x, y) means the same cells on both sides."""

from dataclasses import dataclass
from functools import cached_property

Cell = tuple[int, int]
Shape = tuple[Cell, ...]


def normalize(cells) -> Shape:
    min_x = min(x for x, _ in cells)
    min_y = min(y for _, y in cells)
    return tuple(sorted((x - min_x, y - min_y) for x, y in cells))


def all_orientations(cells) -> tuple[Shape, ...]:
    found, current = set(), normalize(cells)
    for _ in range(2):
        for _ in range(4):
            found.add(current)
            current = normalize([(y, -x) for x, y in current])
        current = normalize([(-x, y) for x, y in current])
    return tuple(sorted(found))


@dataclass(frozen=True)
class PieceSpec:
    name: str
    cells: Shape
    min_side: int
    max_side: int
    dead_sides: frozenset[int]
    use_wall_lemmas: bool
    # the gun's 20 | WH and odd-side rules; otherwise only area divisibility
    gun_coloring: bool

    @property
    def area(self) -> int:
        return len(self.cells)

    @cached_property
    def orientations(self) -> tuple[Shape, ...]:
        return all_orientations(self.cells)

    def admissible(self, width: int, height: int) -> bool:
        """Necessary coloring conditions only."""
        if self.gun_coloring:
            if (width * height) % 20:
                return False
            if width % 2 and height % 8:
                return False
            return not (height % 2 and width % 8)
        return (width * height) % self.area == 0

    def exact_dead(self, width: int, height: int) -> bool:
        return any(
            side < self.min_side or side in self.dead_sides for side in (width, height)
        )

    def candidate_sizes(self, max_side: int | None = None) -> list[tuple[int, int]]:
        """Every W <= H that is admissible and not already excluded exactly."""
        top = max_side or self.max_side
        return [
            (w, h)
            for w in range(self.min_side, top + 1)
            for h in range(w, top + 1)
            if self.admissible(w, h) and not self.exact_dead(w, h)
        ]


GUN = PieceSpec(
    name="gun",
    cells=normalize([(x, 0) for x in range(7)] + [(1, 1), (2, 1), (3, 1)]),
    min_side=137,
    max_side=240,
    dead_sides=frozenset({141, 142, 143}),
    use_wall_lemmas=True,
    gun_coloring=True,
)

# the 7-cell sibling B(1.2^2.1^2); it does tile rectangles (smallest 19 x 28), so it exercises the "possible" path
SIBLING = PieceSpec(
    name="sib",
    cells=normalize([(x, 0) for x in range(5)] + [(1, 1), (2, 1)]),
    min_side=10,
    max_side=40,
    dead_sides=frozenset(),
    use_wall_lemmas=False,
    gun_coloring=False,
)

PIECES = {piece.name: piece for piece in (GUN, SIBLING)}


def piece_by_name(name: str) -> PieceSpec:
    if name not in PIECES:
        raise ValueError(f"unknown piece {name!r}, expected one of {sorted(PIECES)}")
    return PIECES[name]
