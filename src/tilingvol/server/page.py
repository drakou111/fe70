"""The public page: size map, leaderboard, recent results. Every user-supplied string is HTML-escaped."""

import math
import time
from html import escape

from tilingvol.server.config import ServerConfig
from tilingvol.server.db import Database
from tilingvol.server.scheduler import Scheduler
from tilingvol.shared.protocol import (
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
)

CELL = 6
MARGIN = 32
MAP_BELOW_MIN = 10
FAST_S, SLOW_S = 300.0, 10800.0
LIGHT, DARK = (178, 242, 187), (8, 82, 28)
RECENT = 20

CATEGORIES = {
    "coloring": ("#d4d4d4", "Impossible by coloring"),
    "exact": ("#3d6fd1", "Impossible: exact strip results"),
    "impossible": ("#2f9e44", "Impossible: complete search (shade = time)"),
    "possible": ("#ffd43b", "TILEABLE: a tiling was found"),
    "timed_out": ("#f08c00", "Undecided: search timed out"),
    "flagged": ("#e03131", "Waiting for a second check"),
    "running": ("#9775fa", "Running now"),
    "todo": ("#ffffff", "Not searched yet"),
}

STYLE = """
:root { --bg: #fafaf7; --fg: #1d2420; --muted: #5f6b66; --line: #d9ddd9; }
@media (prefers-color-scheme: dark) { :root { --bg: #161a18; --fg: #e8ece9; --muted: #9aa5a0; --line: #2f3633; } }
body { background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; margin: 0 auto;
       max-width: 900px; padding: 16px; }
h1 { font-size: 1.5rem; margin-bottom: 0; } h2 { font-size: 1.15rem; margin-top: 2rem; }
p.lead { color: var(--muted); margin-top: .25rem; }
table { border-collapse: collapse; width: 100%; } td, th { padding: 4px 8px; border-bottom: 1px solid var(--line);
       text-align: left; } td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.wrap { overflow-x: auto; } .found { background: #ffd43b; color: #1d2420; padding: 8px 12px; border-radius: 6px; }
.chip { display: inline-block; width: 12px; height: 12px; border: 1px solid #889; vertical-align: middle; }
"""


def time_shade(seconds: float) -> str:
    t = (math.log(max(seconds, FAST_S)) - math.log(FAST_S)) / (
        math.log(SLOW_S) - math.log(FAST_S)
    )
    t = min(1.0, max(0.0, t))
    r, g, b = (round(lo + (hi - lo) * t) for lo, hi in zip(LIGHT, DARK))
    return f"#{r:02x}{g:02x}{b:02x}"


def hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}"


def render(db: Database, scheduler: Scheduler, config: ServerConfig) -> str:
    piece = config.piece
    results = db.results()
    summaries = scheduler.summaries(results)
    running = {
        (min(j.width, j.height), max(j.width, j.height)) for j in db.active_jobs()
    }

    def category(w: int, h: int) -> tuple[str, str, str]:
        key = (min(w, h), max(w, h))
        if not piece.admissible(*key):
            return (
                "coloring",
                CATEGORIES["coloring"][0],
                "fails the coloring conditions",
            )
        if piece.exact_dead(*key):
            return (
                "exact",
                CATEGORIES["exact"][0],
                "excluded by the exact strip results",
            )
        s = summaries.get(key)
        if s is not None and s.verdict == VERDICT_POSSIBLE:
            return "possible", CATEGORIES["possible"][0], "A TILING EXISTS"
        if s is not None and s.verdict == VERDICT_IMPOSSIBLE:
            return (
                "impossible",
                time_shade(s.duration_s),
                f"impossible, {hours(s.duration_s)} h of search",
            )
        if key in running:
            return "running", CATEGORIES["running"][0], "running now"
        if s is not None and s.flagged:
            return "flagged", CATEGORIES["flagged"][0], "waiting for a second check"
        if s is not None and s.timed_out:
            return (
                "timed_out",
                CATEGORIES["timed_out"][0],
                f"timed out after {hours(s.duration_s)} h",
            )
        return "todo", CATEGORIES["todo"][0], "not searched yet"

    low, high = piece.min_side - MAP_BELOW_MIN, piece.max_side
    n = high - low + 1
    size = MARGIN + n * CELL + 4
    counts = {k: 0 for k in CATEGORIES}
    svg_open = (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" '
        f'aria-label="Status of every rectangle size" style="background:#fff;font:9px sans-serif">'
    )
    svg = [svg_open]
    for i, h in enumerate(range(low, high + 1)):
        for j, w in enumerate(range(low, high + 1)):
            cat, fill, why = category(w, h)
            if w <= h:
                counts[cat] += 1
            x, y = MARGIN + j * CELL, MARGIN + (n - 1 - i) * CELL
            stroke = "#666" if cat == "todo" else "#fff"
            svg.append(
                f'<rect x="{x}" y="{y}" width="{CELL - 1}" height="{CELL - 1}" fill="{fill}" '
                f'stroke="{stroke}" stroke-width="0.4"><title>{w} x {h}: {why}</title></rect>'
            )
    for v in range(low, high + 1, 10):
        k = v - low
        svg.append(
            f'<text x="{MARGIN + k * CELL + CELL / 2}" y="{MARGIN - 4}" text-anchor="middle">{v}</text>'
        )
        svg.append(
            f'<text x="{MARGIN - 4}" y="{MARGIN + (n - 1 - k) * CELL + CELL - 1}" text-anchor="end">{v}</text>'
        )
    svg.append("</svg>")
    legend = "".join(
        f'<tr><td><span class="chip" style="background:{c}"></span></td><td>{label}</td>'
        f'<td class="num">{counts[k]}</td></tr>'
        for k, (c, label) in CATEGORIES.items()
    )

    undecided = sorted(
        (w * h, w, h)
        for w, h in scheduler.sizes
        if not (summaries.get((w, h)) and summaries[(w, h)].decided)
    )
    bound_html = ""
    if undecided:
        area, w, h = undecided[0]
        cap = piece.min_side * (piece.max_side + 1)
        bound = min(area, cap)
        bound_html = (
            f"<p>Every rectangle with area below <b>{bound:,}</b> is ruled out. The smallest open size is "
            f"{w} x {h}.</p>"
        )

    found = [(w, h) for (w, h), s in summaries.items() if s.verdict == VERDICT_POSSIBLE]
    found_html = "".join(
        f'<p class="found">A tiling of {w} x {h} has been found! '
        f'<a href="/api/solution/{w}x{h}">Download it</a>.</p>'
        for w, h in sorted(found)
    )

    board: dict[str, list[float]] = {}
    for r in results:
        if r.flagged:
            continue
        row = board.setdefault(r.name, [0, 0, 0, 0.0])
        row[0] += r.verdict == VERDICT_IMPOSSIBLE
        row[1] += r.verdict == VERDICT_TIMED_OUT
        row[2] += r.verdict == VERDICT_POSSIBLE
        row[3] += r.duration_s
    leaders = sorted(
        board.items(), key=lambda item: (-item[1][2], -item[1][0], -item[1][3])
    )
    leader_rows = "".join(
        f'<tr><td>{escape(name)}</td><td class="num">{int(v[0])}</td><td class="num">{int(v[1])}'
        f'</td><td class="num">{int(v[2])}</td><td class="num">{hours(v[3])}</td></tr>'
        for name, v in leaders
    )

    recent = [r for r in results if r.source == "volunteer"][-RECENT:][::-1]
    recent_rows = "".join(
        f"<tr><td>{r.width} x {r.height}</td><td>{escape(r.verdict.replace('_', ' '))}"
        f'{" (flagged)" if r.flagged else ""}</td><td>{escape(r.stage_tag)}</td><td class="num">{hours(r.duration_s)}'
        f"</td><td>{escape(r.name)}</td><td>{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r.submitted))} UTC</td></tr>"
        for r in recent
    )

    download_html = ""
    if config.download_dir is not None and config.download_dir.is_dir():
        links = ", ".join(
            f'<a href="/download/{escape(p.name)}">{escape(p.name)}</a>'
            for p in sorted(config.download_dir.iterdir())
            if p.is_file()
        )
        if links:
            download_html = f"<h2>Join the search</h2><p>Download the app, enter a name and press Start: {links}</p>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gun Tiling Search</title><style>{STYLE}</style></head><body>
<h1>Does the gun tile a rectangle?</h1>
<p class="lead">A volunteer search for a rectangle tiled by the 10-cell polyomino B(1.2^3.1^3), the last open case
of Dahlke's gun theorem. Each square below is one rectangle size.</p>
{found_html}{bound_html}{download_html}
<h2>Rectangle sizes</h2>
<div class="wrap">{"".join(svg)}</div>
<div class="wrap"><table><tr><th></th><th>Status</th><th class="num">Sizes (W &le; H)</th></tr>{legend}</table></div>
<h2>Leaderboard</h2>
<div class="wrap"><table><tr><th>Name</th><th class="num">Impossible</th><th class="num">Timed out</th>
<th class="num">Tiled</th><th class="num">Hours</th></tr>{leader_rows}</table></div>
<h2>Recent results</h2>
<div class="wrap"><table><tr><th>Size</th><th>Result</th><th>Stage</th><th class="num">Hours</th><th>Name</th>
<th>When</th></tr>{recent_rows}</table></div>
</body></html>"""
