"""Half-strip depth results found outside the app, added to the database when the server starts (each
only once). zach's runs use strip_depth_cpsat.py, Drakou111's halfstrip_depth.py of the research repo;
both cover the first h rows of the width-W half-strip and let pieces stick out above, like the app."""

from dataclasses import dataclass

from tilingvol.server.db import Database
from tilingvol.shared.protocol import VERDICT_FILLED, VERDICT_IMPOSSIBLE


@dataclass(frozen=True)
class KnownStrip:
    name: str
    width: int
    rows: int
    verdict: str
    duration_s: float


GUN_STRIPS = (
    KnownStrip("zach", 135, 48, VERDICT_IMPOSSIBLE, 6306.54),
    KnownStrip("zach", 141, 48, VERDICT_IMPOSSIBLE, 8618.05),
    KnownStrip("zach", 142, 40, VERDICT_IMPOSSIBLE, 6539.76),
    KnownStrip("zach", 143, 40, VERDICT_IMPOSSIBLE, 8625.54),
    KnownStrip("zach", 137, 88, VERDICT_FILLED, 0.0),
    KnownStrip("zach", 138, 88, VERDICT_FILLED, 0.0),
    KnownStrip("zach", 139, 64, VERDICT_FILLED, 0.0),
    KnownStrip("zach", 144, 112, VERDICT_FILLED, 0.0),
    KnownStrip("Drakou111", 140, 40, VERDICT_FILLED, 114.0),
    KnownStrip("Drakou111", 150, 30, VERDICT_FILLED, 32.0),
    KnownStrip("Drakou111", 160, 44, VERDICT_FILLED, 1449.0),
    KnownStrip("Drakou111", 168, 60, VERDICT_FILLED, 2331.0),
    KnownStrip("Drakou111", 176, 80, VERDICT_FILLED, 6660.0),
    KnownStrip("Drakou111", 200, 40, VERDICT_FILLED, 1655.0),
)
KNOWN = {"gun": GUN_STRIPS}


def seed(db: Database, piece_name: str) -> int:
    """Add the known results for this piece that are not in the database yet; returns how many."""
    return sum(
        db.import_strip_result(k.width, k.rows, k.verdict, k.duration_s, k.name)
        for k in KNOWN.get(piece_name, ())
    )
