"""Half-strip results found outside the app, added to the database when the server starts (each only
once). zach's runs use strip_depth_cpsat.py, Drakou111's halfstrip_depth.py of the research repo,
except 137's and 139's deaths, which are exact (cpp/hsw.cpp of the research repo). All cover the first h rows of
the width-W half-strip and let pieces stick out above, like the app.

Fill-forever certificates live in certificates/<piece>-<width>.json and are checked cell by cell before they
are stored. 208's comes from the research repo's arrow_cpsat.py (a filling of rows 0..59 whose frontier is
pair-aligned, so each column pair goes on alone as a period-10 helix; periodic208.py writes it out).

Exact decisions run outside the app (exact_width by hand) are stored like a client's exact result."""

import json
from dataclasses import dataclass
from pathlib import Path

from tilingvol.server.db import Database
from tilingvol.shared.pieces import PieceSpec
from tilingvol.shared.protocol import EXACT_DEAD, VERDICT_FILLED, VERDICT_IMPOSSIBLE
from tilingvol.shared.verify import verify_periodic

CERTIFICATE_DIR = Path(__file__).resolve().parent / "certificates"


@dataclass(frozen=True)
class KnownStrip:
    name: str
    width: int
    rows: int
    verdict: str
    duration_s: float


@dataclass(frozen=True)
class KnownCertificate:
    name: str
    width: int
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
    KnownStrip("Drakou111", 137, 175, VERDICT_IMPOSSIBLE, 10361.0),
    KnownStrip("Drakou111", 139, 65, VERDICT_IMPOSSIBLE, 6663.0),
    KnownStrip("Drakou111", 140, 40, VERDICT_FILLED, 114.0),
    KnownStrip("Drakou111", 150, 30, VERDICT_FILLED, 32.0),
    KnownStrip("Drakou111", 160, 44, VERDICT_FILLED, 1449.0),
    KnownStrip("Drakou111", 168, 60, VERDICT_FILLED, 2331.0),
    KnownStrip("Drakou111", 176, 80, VERDICT_FILLED, 6660.0),
    KnownStrip("Drakou111", 200, 40, VERDICT_FILLED, 1655.0),
)


@dataclass(frozen=True)
class KnownExact:
    name: str
    width: int
    outcome: str
    row: int
    first_row: int
    lookahead: int
    compute_s: float
    counts: tuple[
        int, ...
    ]  # minimal DFA states per row from first_row, as the program prints them


GUN_CERTIFICATES = (KnownCertificate("Drakou111", 208, 3581.0),)
GUN_EXACT = (
    KnownExact(
        "zach",
        175,
        EXACT_DEAD,
        58,
        8,
        6,
        45087.0,
        (
            15681186,
            123392342,
            130781516,
            742375366,
            694396081,
            2616008437,
            2150696263,
            4809366712,
            3574524842,
            5611252429,
            4063840078,
            3203562665,
            2279766162,
            1426766655,
            1172092814,
            627039492,
            556585152,
            184398983,
            173816805,
            66771512,
            65196098,
            10904565,
            10318914,
            1671508,
            1547141,
            211397,
            180929,
            44696,
            20368,
            14746,
            12821,
            10072,
            6632,
            6397,
            2846,
            3030,
            2692,
            3026,
            2100,
            2284,
            1834,
            1772,
            1782,
            1698,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
        ),
    ),
)
KNOWN = {"gun": GUN_STRIPS}
KNOWN_CERTIFICATES = {"gun": GUN_CERTIFICATES}
KNOWN_EXACT = {"gun": GUN_EXACT}


def seed(db: Database, piece: PieceSpec) -> int:
    """Add the known results for this piece that are not in the database yet; returns how many."""
    added = sum(
        db.import_strip_result(k.width, k.rows, k.verdict, k.duration_s, k.name)
        for k in KNOWN.get(piece.name, ())
    )
    for known in KNOWN_CERTIFICATES.get(piece.name, ()):
        data = json.loads(
            (CERTIFICATE_DIR / f"{piece.name}-{known.width}.json").read_text(
                encoding="utf-8"
            )
        )
        if data["width"] != known.width:
            raise ValueError(
                f"certificate file for {known.width} is for width {data['width']}"
            )
        verify_periodic(
            piece, known.width, data["start"], data["period"], data["placements"]
        )
        added += db.import_periodic_certificate(
            known.width,
            data["start"],
            data["period"],
            known.duration_s,
            known.name,
            data["placements"],
        )
    for e in KNOWN_EXACT.get(piece.name, ()):
        added += db.import_exact_result(
            width=e.width,
            outcome=e.outcome,
            row=e.row,
            repeat_from=0,
            first_row=e.first_row,
            counts=e.counts,
            compute_s=e.compute_s,
            lookahead=e.lookahead,
            name=e.name,
        )
    return added
