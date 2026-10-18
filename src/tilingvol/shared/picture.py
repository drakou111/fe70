"""Pictures of border fillings and tilings: every piece colored by its orientation, with thin dark lines
between pieces; cells no piece covers stay dark. Pure Python PNG output (zlib), used by the app and the
server alike."""

import struct
import zlib

from tilingvol.shared.pieces import PieceSpec

SCALE = 4  # pixels per cell; the last row/column of each cell becomes a separating line at piece edges
PALETTE = (
    (220, 64, 64),
    (64, 128, 230),
    (52, 168, 90),
    (160, 112, 240),
    (238, 160, 44),
    (40, 190, 196),
    (214, 196, 52),
    (120, 82, 206),
)
EMPTY = (34, 38, 36)
EDGE = (24, 26, 25)


def piece_grid(
    piece: PieceSpec, width: int, height: int, placements: list[list[int]]
) -> list[int]:
    """Cell -> index of the covering placement (-1 when empty). Assumes a verified, overlap-free list."""
    grid = [-1] * (width * height)
    for index, (orientation, x0, y0) in enumerate(placements):
        for dx, dy in piece.orientations[orientation]:
            grid[(y0 + dy) * width + x0 + dx] = index
    return grid


def render_rgb(
    piece: PieceSpec, width: int, height: int, placements: list[list[int]]
) -> tuple[int, int, bytes]:
    """(pixel width, pixel height, RGB bytes); row 0 of the rectangle is drawn at the bottom."""
    grid = piece_grid(piece, width, height, placements)
    colors = [PALETTE[p[0] % len(PALETTE)] for p in placements]
    out_w, out_h = width * SCALE, height * SCALE
    rows = []
    for py in range(out_h):
        y = height - 1 - py // SCALE
        sub_y = py % SCALE
        row = bytearray()
        for x in range(width):
            owner = grid[y * width + x]
            base = EMPTY if owner < 0 else colors[owner]
            below = grid[(y - 1) * width + x] if y > 0 else -2
            right = grid[y * width + x + 1] if x + 1 < width else -2
            bottom_edge = sub_y == SCALE - 1 and below != owner
            for sub_x in range(SCALE):
                edge = bottom_edge or (sub_x == SCALE - 1 and right != owner)
                row += bytes(EDGE if edge and owner >= 0 else base)
        rows.append(bytes(row))
    return out_w, out_h, b"".join(rows)


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render_png(
    piece: PieceSpec, width: int, height: int, placements: list[list[int]]
) -> bytes:
    return encode_png(*render_rgb(piece, width, height, placements))
