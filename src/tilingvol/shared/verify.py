from tilingvol.shared.pieces import PieceSpec


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
