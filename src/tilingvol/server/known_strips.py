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
from tilingvol.shared.protocol import (
    EXACT_DEAD,
    EXACT_FOREVER,
    VERDICT_FILLED,
    VERDICT_IMPOSSIBLE,
)
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
    # minimal DFA states per row from first_row, as the program prints them
    counts: tuple[int, ...]
    repeat_from: int = 0  # EXACT_FOREVER: the earlier row that `row` repeats
    note: str = ""  # shown with an asterisk: how the result falls short of a full exact decision


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
    KnownExact(
        "zach",
        176,
        EXACT_FOREVER,
        229,
        8,
        6,
        32894.0,
        (
            15950848,
            125211417,
            132231438,
            744596092,
            681467434,
            2415880652,
            1886814321,
            3886010342,
            2871600273,
            3847153411,
            2658663634,
            1842948196,
            1351406319,
            705223912,
            559975474,
            243761741,
            215349714,
            68700296,
            62905356,
            17099172,
            15291798,
            2377956,
            1908624,
            1037438,
            813234,
            721778,
            551365,
            532607,
            390868,
            398940,
            340251,
            319438,
            274512,
            271785,
            249201,
            252843,
            232244,
            239049,
            216621,
            227465,
            180379,
            174683,
            158666,
            159235,
            146136,
            149502,
            142546,
            147291,
            134186,
            140740,
            128671,
            119695,
            113258,
            117429,
            114515,
            116364,
            114826,
            120526,
            116024,
            122863,
            114988,
            112001,
            107405,
            107043,
            104644,
            109454,
            105949,
            113058,
            103183,
            111019,
            105250,
            104979,
            100977,
            103346,
            102075,
            102119,
            98573,
            104397,
            95773,
            103738,
            97597,
            96267,
            90918,
            93467,
            91442,
            92547,
            89479,
            95381,
            86723,
            94421,
            87787,
            88789,
            83699,
            86870,
            84880,
            86535,
            83791,
            87752,
            78014,
            85233,
            79074,
            82519,
            77560,
            81808,
            79101,
            80996,
            78498,
            83964,
            73367,
            80157,
            74669,
            78606,
            74031,
            78534,
            76236,
            79115,
            76532,
            82208,
            71780,
            78429,
            72772,
            74296,
            69887,
            73741,
            71695,
            74662,
            72129,
            76906,
            66773,
            73281,
            67929,
            69771,
            65795,
            70061,
            67981,
            70987,
            68583,
            73847,
            63729,
            70110,
            64277,
            67874,
            63734,
            67890,
            65970,
            69097,
            66797,
            71937,
            61863,
            67913,
            62270,
            66274,
            62040,
            66076,
            64158,
            67145,
            64863,
            69869,
            60058,
            66006,
            60587,
            64311,
            60465,
            64754,
            62980,
            66255,
            64019,
            69011,
            59224,
            65136,
            59989,
            63631,
            59787,
            64032,
            62320,
            65553,
            63349,
            68337,
            58580,
            64732,
            59583,
            63267,
            59413,
            63640,
            61970,
            65183,
            63005,
            67989,
            58248,
            64396,
            59234,
            62893,
            59040,
            63252,
            61608,
            64801,
            62642,
            67619,
            57894,
            64030,
            58873,
            62487,
            58633,
            62826,
            61211,
            64388,
            62252,
            67226,
            57521,
            63644,
            58468,
            62053,
            58201,
            62374,
            60808,
            63967,
            61842,
            66800,
            57053,
            63157,
            57999,
            62053,
        ),
        repeat_from=219,
        note="rows 219 and 229 matched by their 128-bit hash; an older app version stopped before"
        " comparing them exactly",
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
            repeat_from=e.repeat_from,
            first_row=e.first_row,
            counts=e.counts,
            compute_s=e.compute_s,
            lookahead=e.lookahead,
            name=e.name,
            note=e.note,
        )
        if e.outcome == EXACT_DEAD:
            # as for an app's exact death: the strip result is what takes the width off the map
            added += db.import_strip_result(
                e.width, e.row, VERDICT_IMPOSSIBLE, e.compute_s, e.name
            )
    return added
