"""Piece definitions shared by client and server. Orientation order is canonical (sorted), so a placement
(orientation, x, y) means the same cells on both sides."""

from dataclasses import dataclass
from functools import cached_property

Cell = tuple[int, int]
Shape = tuple[Cell, ...]

MAX_LIVE_WIDTH = 1000
EXACT_CYLINDER_MAX_PERIOD = 54


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


def live_parts_table(bases, top: int = MAX_LIVE_WIDTH) -> dict[int, tuple[int, ...]]:
    """width -> fill-forever widths from `bases` summing to it (largest first), for widths up to `top`."""
    table: dict[int, tuple[int, ...]] = {}
    for width in range(1, top + 1):
        for base in sorted(bases, reverse=True):
            if base == width:
                table[width] = (base,)
                break
            rest = table.get(width - base)
            if rest is not None:
                table[width] = tuple(sorted((base, *rest), reverse=True))
                break
    return table


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
    # half-strip widths known to fill forever; side by side, their sums fill forever too
    live_bases: frozenset[int] = frozenset()
    # rows the exact decider builds for all widths at once before it turns to one width (cheap for the
    # gun up to 8; a piece with more frontier shapes needs fewer)
    exact_start_row: int = 8

    @property
    def area(self) -> int:
        return len(self.cells)

    @cached_property
    def orientations(self) -> tuple[Shape, ...]:
        return all_orientations(self.cells)

    @cached_property
    def side_prime(self) -> int:
        """The largest prime factor of the area: a W x H rectangle has W * H divisible by it, so one of its
        sides is (for the gun, 5)."""
        n, p, largest = self.area, 2, 1
        while p * p <= n:
            while n % p == 0:
                n, largest = n // p, p
            p += 1
        return max(largest, n)

    @cached_property
    def strip_reach(self) -> int:
        """How far a piece can stick out above its lowest row."""
        return max(max(y for _, y in shape) for shape in self.orientations)

    def admissible(self, width: int, height: int) -> bool:
        """Necessary coloring conditions only."""
        if self.gun_coloring:
            if (width * height) % 20:
                return False
            if width % 2 and height % 8:
                return False
            return not (height % 2 and width % 8)
        return (width * height) % self.area == 0

    def live_parts(self, width: int) -> tuple[int, ...] | None:
        """Known fill-forever widths that sum to `width` (largest first), or None if there are none.
        Placing half-strip fillings side by side fills the wider half-strip, so a strip search at such a
        width can never end in a death."""
        return self._live_parts_table.get(width)

    @cached_property
    def _live_parts_table(self) -> dict[int, tuple[int, ...]]:
        return live_parts_table(self.live_bases)

    def periodic_ruled_out(self, width: int, period: int) -> bool:
        """A filling of the width-W half-strip repeating every P rows, with its block glued top to bottom, is
        a cylinder of length W and circumference P between two walls. For the gun (research repo,
        PROGRESS.md, walled cylinders): those exist only for P divisible by 10 and W even when P <= 54
        (exact, every W), and an odd W needs 20 | P at any P (a column-parity weighting)."""
        if (width * period) % self.area:
            return True
        if not self.gun_coloring:
            return False
        if period <= EXACT_CYLINDER_MAX_PERIOD:
            return period % 10 != 0 or width % 2 == 1 or width < 8
        return width % 2 == 1 and period % 20 != 0

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
    max_side=300,
    dead_sides=frozenset(),  # widths ruled out by strip searches live in the server's database
    use_wall_lemmas=True,
    gun_coloring=True,
    # exact hsw computation (research repo, output/halfstrip_widths.txt; periodic certificates in
    # certificates/); 176, 184, 192 and 280 have their own certificates but are sums of these
    live_bases=frozenset({88, 96, 128, 136}),
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
    exact_start_row=4,
)

PIECES = {piece.name: piece for piece in (GUN, SIBLING)}


def piece_by_name(name: str) -> PieceSpec:
    if name not in PIECES:
        raise ValueError(f"unknown piece {name!r}, expected one of {sorted(PIECES)}")
    return PIECES[name]
