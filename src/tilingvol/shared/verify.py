from tilingvol.shared.pieces import PieceSpec
from tilingvol.shared.protocol import MAX_PERIOD, MIN_PERIOD

PERIODIC_COPIES = 3


class TilingError(ValueError):
    pass


def verify_tiling(
    piece: PieceSpec, width: int, height: int, placements: list[list[int]]
) -> None:
    """Raise TilingError unless the placements [orientation, x, y] tile the W x H rectangle exactly."""
    if width <= 0 or height <= 0:
        raise TilingError("rectangle sides must be positive")
    if len(placements) * piece.area != width * height:
        raise TilingError(
            f"{len(placements)} pieces cannot cover {width * height} cells"
        )
    shapes = piece.orientations
    grid = bytearray(width * height)
    for index, placement in enumerate(placements):
        if len(placement) != 3 or not all(isinstance(v, int) for v in placement):
            raise TilingError(f"piece {index}: expected [orientation, x, y]")
        orientation, x0, y0 = placement
        if not 0 <= orientation < len(shapes):
            raise TilingError(f"piece {index}: orientation {orientation} out of range")
        for dx, dy in shapes[orientation]:
            x, y = x0 + dx, y0 + dy
            if not (0 <= x < width and 0 <= y < height):
                raise TilingError(
                    f"piece {index}: cell ({x}, {y}) outside the rectangle"
                )
            cell = y * width + x
            if grid[cell]:
                raise TilingError(f"piece {index}: cell ({x}, {y}) covered twice")
            grid[cell] = 1
    # the count check plus no overlaps already implies full cover; kept explicit for safety
    if not all(grid):
        raise TilingError("some cells are uncovered")


def verify_border(
    piece: PieceSpec, width: int, height: int, border: int, placements: list[list[int]]
) -> None:
    """Raise TilingError unless the placements cover every cell within `border` of the edge exactly once
    (inner cells at most once): a genuine border filling, as the border stage of the search finds."""
    if not 1 <= border <= min(width, height) // 2:
        raise TilingError("border depth out of range")
    shapes = piece.orientations
    grid = bytearray(width * height)
    for index, placement in enumerate(placements):
        if len(placement) != 3 or not all(isinstance(v, int) for v in placement):
            raise TilingError(f"piece {index}: expected [orientation, x, y]")
        orientation, x0, y0 = placement
        if not 0 <= orientation < len(shapes):
            raise TilingError(f"piece {index}: orientation {orientation} out of range")
        for dx, dy in shapes[orientation]:
            x, y = x0 + dx, y0 + dy
            if not (0 <= x < width and 0 <= y < height):
                raise TilingError(
                    f"piece {index}: cell ({x}, {y}) outside the rectangle"
                )
            if grid[y * width + x]:
                raise TilingError(f"piece {index}: cell ({x}, {y}) covered twice")
            grid[y * width + x] = 1
    for y in range(height):
        for x in range(width):
            if (
                min(x, y, width - 1 - x, height - 1 - y) < border
                and not grid[y * width + x]
            ):
                raise TilingError(f"border cell ({x}, {y}) is not covered")


def verify_symmetric(
    piece: PieceSpec, width: int, height: int, placements: list[list[int]]
) -> None:
    """Raise TilingError unless turning the placements by 180 degrees gives the same set of pieces (the
    placements must already be valid, e.g. checked by verify_border)."""
    shapes = piece.orientations
    pieces = {
        frozenset((x0 + dx, y0 + dy) for dx, dy in shapes[o])
        for o, x0, y0 in placements
    }
    for cells in pieces:
        if frozenset((width - 1 - x, height - 1 - y) for x, y in cells) not in pieces:
            x, y = min(cells)
            raise TilingError(f"the piece at ({x}, {y}) has no turned twin")


def verify_strip(
    piece: PieceSpec, width: int, rows: int, placements: list[list[int]]
) -> None:
    """Raise TilingError unless the placements cover rows 0..rows-1 of the width-W half-strip exactly once,
    with every piece starting in those rows (it may stick out above them) and no two pieces overlapping."""
    if width <= 0 or rows <= 0:
        raise TilingError("strip width and depth must be positive")
    shapes = piece.orientations
    top = rows + piece.strip_reach
    grid = bytearray(width * top)
    for index, placement in enumerate(placements):
        if len(placement) != 3 or not all(isinstance(v, int) for v in placement):
            raise TilingError(f"piece {index}: expected [orientation, x, y]")
        orientation, x0, y0 = placement
        if not 0 <= orientation < len(shapes):
            raise TilingError(f"piece {index}: orientation {orientation} out of range")
        if not 0 <= y0 < rows:
            raise TilingError(
                f"piece {index}: it does not start in the first {rows} rows"
            )
        for dx, dy in shapes[orientation]:
            x, y = x0 + dx, y0 + dy
            if not 0 <= x < width:
                raise TilingError(f"piece {index}: cell ({x}, {y}) outside the strip")
            if grid[y * width + x]:
                raise TilingError(f"piece {index}: cell ({x}, {y}) covered twice")
            grid[y * width + x] = 1
    if not all(grid[: rows * width]):
        cell = grid.index(0)
        raise TilingError(f"cell ({cell % width}, {cell // width}) is not covered")


def verify_periodic(
    piece: PieceSpec, width: int, start: int, period: int, placements: list[list[int]]
) -> None:
    """Raise TilingError unless the placements, all starting below row start + period, fill the width-W
    half-strip forever when the block of pieces starting in rows [start, start + period) repeats upward.

    Expand the prefix plus three copies of the block and check rows below start + 2 * period exactly, with
    no overlap anywhere. That is enough: a piece spans at most 7 <= period rows, so the prefix reaches only
    the first window [start, start + period), and every later window [start + j*period, ...) is covered by
    copies j-1 and j exactly as window one is by copies 0 and 1; copies two apart never meet."""
    if width <= 0:
        raise TilingError("strip width must be positive")
    if not MIN_PERIOD <= period <= MAX_PERIOD or start < piece.strip_reach:
        raise TilingError("periodic start or period out of range")
    shapes = piece.orientations
    top = start + period
    prefix, block = [], []
    for index, placement in enumerate(placements):
        if len(placement) != 3 or not all(isinstance(v, int) for v in placement):
            raise TilingError(f"piece {index}: expected [orientation, x, y]")
        orientation, x0, y0 = placement
        if not 0 <= orientation < len(shapes):
            raise TilingError(f"piece {index}: orientation {orientation} out of range")
        if not 0 <= y0 < top:
            raise TilingError(f"piece {index}: it does not start below row {top}")
        (prefix if y0 < start else block).append(placement)
    pieces = prefix + [
        [o, x0, y0 + copy * period]
        for copy in range(PERIODIC_COPIES)
        for o, x0, y0 in block
    ]
    checked = start + 2 * period
    height = start + PERIODIC_COPIES * period + piece.strip_reach
    grid = bytearray(width * height)
    for index, (orientation, x0, y0) in enumerate(pieces):
        for dx, dy in shapes[orientation]:
            x, y = x0 + dx, y0 + dy
            if not 0 <= x < width:
                raise TilingError(f"piece {index}: cell ({x}, {y}) outside the strip")
            if grid[y * width + x]:
                raise TilingError(f"cell ({x}, {y}) covered twice")
            grid[y * width + x] = 1
    if not all(grid[: checked * width]):
        cell = grid.index(0)
        raise TilingError(f"cell ({cell % width}, {cell // width}) is not covered")
