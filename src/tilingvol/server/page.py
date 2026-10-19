"""The public page: headline numbers, download, size map, running jobs, leaderboard, recent results.
Every user-supplied string is HTML-escaped."""

import json
import math
import re
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

from tilingvol.server.config import ServerConfig
from tilingvol.server.db import Database, ResultRow
from tilingvol.server.scheduler import Scheduler
from tilingvol.shared.protocol import (
    LANE_HUNT,
    VERDICT_IMPOSSIBLE,
    VERDICT_POSSIBLE,
    VERDICT_TIMED_OUT,
)

MANIFEST_NAME = "latest.json"
MAX_MANIFEST_BYTES = 64 * 1024
RECENT = 15
GALLERY = 6
HUNT_PILL = '<span class="pill hunt">hunt</span>'
HUNT_FIGURE = ' class="hunt"'
PITCH = 8  # map grid pitch in px; tiles are slightly smaller so they read as separate squares
TILE = 6.5
MAP_LEFT, MAP_TOP, MAP_BOTTOM = 36, 8, 28
TICK_STEP = 20
FAST_S, SLOW_S = 300.0, 10800.0
GREEN_FAST, GREEN_SLOW = (140, 233, 154), (43, 138, 62)
PLATFORM_NAMES = {"windows": "Windows", "linux": "Linux", "macos": "macOS"}

STYLE = """
:root {
  --bg: #f6f7f5; --card: #ffffff; --fg: #1b1f1d; --muted: #66706b; --line: #e3e6e3;
  --todo: #e4e7e4; --timed: #f59f00; --run: #4263eb; --found: #fab005; --flag: #e8590c; --accent: #2b8a3e; --hunt: #7c3aed;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #121514; --card: #1b1f1d; --fg: #e9ecea; --muted: #98a29d; --line: #2c322f; --todo: #2e3431; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 32px 16px 48px; }
header { display: flex; gap: 20px; align-items: center; margin-bottom: 24px; }
header svg { flex: none; }
h1 { font-size: 1.6rem; line-height: 1.2; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 1.05rem; margin: 0 0 12px; }
.lead { color: var(--muted); margin: 0; max-width: 60ch; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 20px; margin-bottom: 16px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.stat b { display: block; font-size: 1.5rem; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.stat span { color: var(--muted); font-size: 0.85rem; }
.join { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: center; justify-content: space-between; }
.join p { margin: 0; color: var(--muted); }
.btn { display: inline-block; background: var(--accent); color: #fff; text-decoration: none; font-weight: 600;
       padding: 10px 18px; border-radius: 8px; }
.btn + .btn { margin-left: 8px; }
.found { background: var(--found); color: #1b1f1d; font-weight: 600; }
.found a { color: inherit; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 14px; padding: 0; list-style: none;
          color: var(--muted); font-size: 0.88rem; }
.legend i { display: inline-block; width: 11px; height: 11px; border-radius: 3px; margin-right: 6px;
            vertical-align: -1px; }
.legend b { color: var(--fg); font-weight: 600; margin-left: 4px; font-variant-numeric: tabular-nums; }
.map { position: relative; }
.map svg { display: block; width: 100%; height: auto; }
.map text { fill: var(--muted); font: 10px system-ui, sans-serif; }
.map .grid { stroke: var(--line); stroke-width: 1; }
.map rect[data-t] { cursor: pointer; }
.map rect[data-t]:hover { stroke: var(--fg); stroke-width: 1.2; }
.tip { position: absolute; pointer-events: none; background: var(--fg); color: var(--bg); font-size: 0.82rem;
       padding: 5px 9px; border-radius: 6px; white-space: nowrap; }
.note { color: var(--muted); font-size: 0.85rem; margin: 12px 0 0; }
table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
th { text-align: left; color: var(--muted); font-weight: 500; font-size: 0.82rem; padding: 0 8px 8px; }
td { padding: 7px 8px; border-top: 1px solid var(--line); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
.empty { color: var(--muted); margin: 0; }
.pill { display: inline-block; font-size: 0.78rem; padding: 1px 8px; border-radius: 99px; background: var(--todo); }
.pill.impossible { background: rgba(43, 138, 62, 0.15); color: var(--accent); }
.pill.timed_out { background: rgba(245, 159, 0, 0.18); color: #b35c00; }
.pill.possible { background: var(--found); color: #1b1f1d; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 720px) { .two { grid-template-columns: 1fr; } header { flex-direction: column; align-items: flex-start; } }
footer { color: var(--muted); font-size: 0.82rem; margin-top: 24px; }
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }
.gallery figure { margin: 0; }
.gallery img { display: block; width: 100%; height: auto; border-radius: 6px; background: #222624; }
.gallery figcaption { color: var(--muted); font-size: 0.8rem; margin-top: 6px; line-height: 1.35; }
.gallery b { color: var(--fg); font-weight: 600; }
.gallery figure.hunt img { outline: 3px solid var(--hunt); outline-offset: 2px; }
.pill.hunt { background: rgba(124, 58, 237, 0.15); color: var(--hunt); }
.hunt-title { color: var(--hunt); }
"""

TOOLTIP_JS = """
const map = document.querySelector('.map'), tip = document.querySelector('.tip');
function show(e) {
  const text = e.target.dataset ? e.target.dataset.t : null;
  if (!text) { tip.hidden = true; return; }
  tip.textContent = text; tip.hidden = false;
  const box = map.getBoundingClientRect();
  let x = e.clientX - box.left + 14, y = e.clientY - box.top + 14;
  if (x + tip.offsetWidth > box.width) x = e.clientX - box.left - tip.offsetWidth - 10;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
map.addEventListener('mousemove', show);
map.addEventListener('click', show);
map.addEventListener('mouseleave', () => { tip.hidden = true; });
"""


@dataclass(frozen=True)
class Tile:
    status: str  # impossible, possible, timed_out, flagged, running, todo
    fill: str
    text: str


def load_manifest(folder: Path | None) -> dict | None:
    if folder is None:
        return None
    path = folder / MANIFEST_NAME
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    platforms = manifest.get("platforms") if isinstance(manifest, dict) else None
    if not isinstance(platforms, dict) or not isinstance(manifest.get("version"), str):
        return None
    if not all(
        isinstance(e, dict) and isinstance(e.get("filename"), str)
        for e in platforms.values()
    ):
        return None
    return manifest


def time_shade(seconds: float) -> str:
    """Light green for quick proofs, deep green for long ones (log scale); readable on light and dark."""
    t = (math.log(max(seconds, FAST_S)) - math.log(FAST_S)) / (
        math.log(SLOW_S) - math.log(FAST_S)
    )
    t = min(1.0, max(0.0, t))
    r, g, b = (round(lo + (hi - lo) * t) for lo, hi in zip(GREEN_FAST, GREEN_SLOW))
    return f"#{r:02x}{g:02x}{b:02x}"


def hours(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def ago(timestamp: float) -> str:
    minutes = max(0, int((time.time() - timestamp) / 60))
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def piece_svg(cells, size: int = 14) -> str:
    width = (max(x for x, _ in cells) + 1) * size
    height = (max(y for _, y in cells) + 1) * size
    squares = "".join(
        f'<rect x="{x * size}" y="{height - (y + 1) * size}" width="{size - 2}" height="{size - 2}" rx="2" '
        f'fill="var(--accent)"/>'
        for x, y in cells
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="the fe70 piece">{squares}</svg>'
    )


def map_note(piece) -> str:
    dead = ", ".join(str(v) for v in sorted(piece.dead_sides))
    exact = f"Sizes with a side below {piece.min_side}" + (
        f" or equal to {dead}" if dead else ""
    )
    coloring = (
        "the coloring conditions (20 divides W x H, and an odd side needs 8 to divide the other)"
        if piece.gun_coloring
        else f"the area condition ({piece.area} divides W x H)"
    )
    return (
        f"Height H goes up, width W across; only W &le; H is shown, and darker green means a longer search. "
        f"{exact} are ruled out by exact strip computations; sizes failing {coloring} cannot be tiled and are "
        f"not drawn."
    )


def picture_caption(p) -> str:
    if p.border == 0:
        return "a complete tiling!"
    prefix = "symmetric " if p.symmetric else ""
    if p.verdict is None:
        return f"{prefix}border {p.border} filled; searching deeper"
    match = re.search(r"frame(\d+)", p.stage_tag)
    deeper = f"border {match[1]}" if match else "the full search"
    outcome = {VERDICT_IMPOSSIBLE: "impossible", VERDICT_TIMED_OUT: "timed out"}.get(
        p.verdict, p.verdict
    )
    return f"{prefix}border {p.border} filled; {deeper} {outcome}"


def deciding_rows(results: list[ResultRow]) -> dict[tuple[int, int], ResultRow]:
    """For each size, the result that settles it (a tiling beats a proof beats a timeout)."""
    rank = {VERDICT_POSSIBLE: 3, VERDICT_IMPOSSIBLE: 2, VERDICT_TIMED_OUT: 1}
    best: dict[tuple[int, int], ResultRow] = {}
    for r in results:
        if r.flagged:
            continue
        key = (min(r.width, r.height), max(r.width, r.height))
        if key not in best or rank.get(r.verdict, 0) > rank.get(best[key].verdict, 0):
            best[key] = r
    return best


def render_hunt(db: Database, scheduler: Scheduler, active: list) -> str:
    """The symmetric hunt keeps its own books: it can find a tiling, but its "impossible" settles nothing."""
    hunt_results = db.results("hunt")
    hunting = [j for j in active if j.lane == LANE_HUNT]
    if not hunt_results and not hunting:
        return ""
    summaries = scheduler.summaries(hunt_results)
    ruled_out = sorted(
        (w * h, w, h)
        for (w, h), s in summaries.items()
        if s.decided and s.verdict == VERDICT_IMPOSSIBLE
    )
    timed_out = sum(1 for s in summaries.values() if s.timed_out and not s.decided)
    largest = f"{ruled_out[-1][1]} x {ruled_out[-1][2]}" if ruled_out else "-"
    stats = [
        (
            f"{len(ruled_out):,}",
            "Sizes with no symmetric tiling",
            f"largest: {largest}",
        ),
        (f"{timed_out:,}", "Timed out", "symmetric search undecided"),
        (f"{len(hunting)}", "Hunting now", "volunteers with the hunt on"),
    ]
    stats_html = "".join(
        f'<div class="stat"><span>{label}</span><b>{value}</b><span>{note}</span></div>'
        for value, label, note in stats
    )
    recent = hunt_results[-RECENT:][::-1]
    rows = "".join(
        f'<tr><td>{r.width} x {r.height}</td><td><span class="pill {escape(r.verdict)}">'
        f"{escape(r.verdict.replace('_', ' '))}</span></td><td>{escape(r.name)}</td>"
        f'<td class="num">{hours(r.duration_s)}</td><td class="num">{ago(r.submitted)}</td></tr>'
        for r in recent
    )
    table = (
        f'<div class="scroll"><table><tr><th>Size</th><th>Symmetric result</th><th>Volunteer</th>'
        f'<th class="num">Search</th><th class="num">When</th></tr>{rows}</table></div>'
        if recent
        else '<p class="empty">No hunt results yet.</p>'
    )
    return (
        '<section class="card"><h2 class="hunt-title">Symmetric hunt</h2><p class="note" style="margin:0 0 12px">'
        "Volunteers who turn on the hunt search only for tilings that look the same after a half turn. "
        "That search is much smaller, so it can reach larger sizes; if it finds a tiling, the question is "
        'answered. "Impossible" here only rules out symmetric tilings, so these results do not change the '
        f'map above.</p><div class="stats">{stats_html}</div>{table}</section>'
    )


def render(db: Database, scheduler: Scheduler, config: ServerConfig) -> str:
    piece = config.piece
    results = db.results()
    summaries = scheduler.summaries(results)
    decisive = deciding_rows(results)
    active = db.active_jobs()
    running = {
        (min(j.width, j.height), max(j.width, j.height)): j
        for j in active
        if j.lane != LANE_HUNT
    }
    sizes = [k for k in scheduler.sizes if k[1] <= piece.max_side]

    def tile(key: tuple[int, int]) -> Tile:
        w, h = key
        s, row = summaries.get(key), decisive.get(key)
        who = f", {row.name}" if row else ""
        if s is not None and s.verdict == VERDICT_POSSIBLE:
            return Tile("possible", "var(--found)", f"{w} x {h}: A TILING EXISTS{who}")
        if s is not None and s.verdict == VERDICT_IMPOSSIBLE:
            return Tile(
                "impossible",
                time_shade(s.duration_s),
                f"{w} x {h}: impossible, {hours(s.duration_s)}{who}",
            )
        if key in running:
            return Tile(
                "running", "var(--run)", f"{w} x {h}: running now ({running[key].name})"
            )
        if s is not None and s.flagged:
            return Tile(
                "flagged", "var(--flag)", f"{w} x {h}: waiting for a second check"
            )
        if s is not None and s.timed_out:
            return Tile(
                "timed_out",
                "var(--timed)",
                f"{w} x {h}: timed out after {hours(s.duration_s)}{who}",
            )
        return Tile("todo", "var(--todo)", f"{w} x {h}: not searched yet")

    tiles = {key: tile(key) for key in sizes}
    counts: dict[str, int] = {}
    for t in tiles.values():
        counts[t.status] = counts.get(t.status, 0) + 1

    # map: W across, H up, only sizes that are still candidates (W <= H, passing the coloring test)
    low, high = piece.min_side, piece.max_side
    span = high - low + 1
    width = MAP_LEFT + span * PITCH + 4
    height = MAP_TOP + span * PITCH + MAP_BOTTOM

    def px(w: int) -> float:
        return MAP_LEFT + (w - low) * PITCH

    def py(h: int) -> float:
        return MAP_TOP + (high - h) * PITCH

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Status of every candidate rectangle size">'
    ]
    first_tick = math.ceil(low / TICK_STEP) * TICK_STEP
    for v in range(first_tick, high + 1, TICK_STEP):
        x, y = px(v) + TILE / 2, py(v) + TILE / 2
        parts.append(
            f'<line class="grid" x1="{x}" y1="{MAP_TOP}" x2="{x}" y2="{py(low) + TILE}"/>'
        )
        parts.append(
            f'<line class="grid" x1="{MAP_LEFT}" y1="{y}" x2="{px(high) + TILE}" y2="{y}"/>'
        )
        parts.append(
            f'<text x="{x}" y="{height - MAP_BOTTOM + 16}" text-anchor="middle">{v}</text>'
        )
        parts.append(
            f'<text x="{MAP_LEFT - 6}" y="{y + 3.5}" text-anchor="end">{v}</text>'
        )
    parts.append(
        f'<text x="{MAP_LEFT + span * PITCH / 2}" y="{height - 2}" text-anchor="middle">width W</text>'
    )
    for (w, h), t in tiles.items():
        parts.append(
            f'<rect x="{px(w)}" y="{py(h)}" width="{TILE}" height="{TILE}" rx="1.6" fill="{t.fill}" '
            f'data-t="{escape(t.text)}"/>'
        )
    parts.append("</svg>")

    legend_items = [
        ("impossible", "var(--accent)", "Impossible"),
        ("timed_out", "var(--timed)", "Timed out"),
        ("running", "var(--run)", "Running now"),
        ("todo", "var(--todo)", "Not searched yet"),
        ("flagged", "var(--flag)", "Being re-checked"),
        ("possible", "var(--found)", "TILED"),
    ]
    legend = "".join(
        f'<li><i style="background:{color}"></i>{label}<b>{counts.get(status, 0):,}</b></li>'
        for status, color, label in legend_items
        if counts.get(status)
        or status in ("impossible", "timed_out", "running", "todo")
    )

    open_sizes = sorted(
        (w * h, w, h)
        for w, h in scheduler.sizes
        if not (summaries.get((w, h)) and summaries[(w, h)].decided)
    )
    bound_value, bound_note = "-", "every size is decided"
    if open_sizes:
        area, w, h = open_sizes[0]
        bound_value = f"{min(area, piece.min_side * (piece.max_side + 1)):,}"
        bound_note = f"smallest open size: {w} x {h}"

    volunteer_names = {r.name for r in results if r.source == "volunteer"} | {
        j.name for j in active
    }
    volunteer_hours = (
        sum(r.duration_s for r in results if r.source == "volunteer") / 3600
    )
    stats = [
        (bound_value, "Area bound", bound_note),
        (f"{counts.get('impossible', 0):,}", "Sizes ruled out", "by complete search"),
        (f"{len(active)}", "Running now", f"{len(volunteer_names)} volunteers"),
        (f"{volunteer_hours:,.0f}", "Volunteer hours", "of search so far"),
    ]
    stats_html = "".join(
        f'<div class="stat"><span>{label}</span><b>{value}</b><span>{note}</span></div>'
        for value, label, note in stats
    )

    found = sorted(k for k, t in tiles.items() if t.status == "possible")
    found_html = "".join(
        f'<div class="card found">A tiling of {w} x {h} has been found! '
        f'<a href="/api/solution/{w}x{h}">Download it</a>.</div>'
        for w, h in found
    )

    join_html = ""
    manifest = load_manifest(config.download_dir)
    if manifest is not None:
        buttons = "".join(
            f'<a class="btn" href="/download/{escape(entry["filename"])}">Download for '
            f"{PLATFORM_NAMES.get(key, escape(key))}</a>"
            for key, entry in sorted(manifest["platforms"].items())
        )
        join_html = (
            f'<div class="card join"><div><h2>Join the search</h2><p>Download version '
            f"{escape(manifest['version'])}, enter a name and press Start. It uses only the RAM and "
            f"CPU you allow, and can be paused any time.</p></div><div>{buttons}</div></div>"
        )

    running_rows = "".join(
        f"<tr><td>{j.width} x {j.height}</td><td>{escape(j.name)}</td><td>"
        f"{HUNT_PILL if j.lane == LANE_HUNT else escape(j.lane)}</td>"
        f'<td class="num">{ago(j.created)}</td></tr>'
        for j in sorted(active, key=lambda j: j.created)
    )
    running_html = (
        f'<div class="scroll"><table><tr><th>Size</th><th>Volunteer</th><th>Order</th>'
        f'<th class="num">Started</th></tr>{running_rows}</table></div>'
        if active
        else '<p class="empty">Nothing is running right now.</p>'
    )

    board: dict[str, list[float]] = {}
    for r in results:
        if r.flagged:
            continue
        row = board.setdefault(r.name, [0, 0, 0.0])
        row[0] += r.verdict in (VERDICT_IMPOSSIBLE, VERDICT_POSSIBLE)
        row[1] += r.verdict == VERDICT_TIMED_OUT
        row[2] += r.duration_s
    leaders = sorted(board.items(), key=lambda item: (-item[1][0], -item[1][2]))
    leader_rows = "".join(
        f'<tr><td class="num">{rank}</td><td>{escape(name)}</td><td class="num">{int(v[0]):,}</td>'
        f'<td class="num">{int(v[1])}</td><td class="num">{v[2] / 3600:,.0f}</td></tr>'
        for rank, (name, v) in enumerate(leaders, 1)
    )
    leader_html = (
        f'<div class="scroll"><table><tr><th class="num">#</th><th>Name</th><th class="num">Settled</th>'
        f'<th class="num">Timed out</th><th class="num">Hours</th></tr>{leader_rows}</table></div>'
        if leaders
        else '<p class="empty">No results yet.</p>'
    )

    pictures = db.recent_pictures(GALLERY)
    figures = "".join(
        f'<figure{HUNT_FIGURE if p.symmetric else ""}><a href="{p.url}"><img src="{p.url}" loading="lazy" '
        f'alt="{p.width} x {p.height}"></a><figcaption><b>{p.width} x {p.height}</b><br>'
        f"{escape(picture_caption(p))}<br>by {escape(p.name)}</figcaption></figure>"
        for p in pictures
    )
    gallery_html = (
        '<section class="card"><h2>Latest pictures</h2><p class="note" style="margin:0 0 12px">'
        "When a border can be filled, the search goes deeper; these are the deepest fillings volunteers "
        f'found, colored by piece orientation.</p><div class="gallery">{figures}</div></section>'
        if pictures
        else ""
    )

    hunt_html = render_hunt(db, scheduler, active)

    recent = [r for r in results if r.source == "volunteer"][-RECENT:][::-1]
    recent_rows = "".join(
        f'<tr><td>{r.width} x {r.height}</td><td><span class="pill {escape(r.verdict)}">'
        f"{escape(r.verdict.replace('_', ' '))}{' (flagged)' if r.flagged else ''}</span></td>"
        f'<td>{escape(r.name)}</td><td class="num">{hours(r.duration_s)}</td>'
        f'<td class="num">{ago(r.submitted)}</td></tr>'
        for r in recent
    )
    recent_html = (
        f'<div class="scroll"><table><tr><th>Size</th><th>Result</th><th>Volunteer</th>'
        f'<th class="num">Search</th><th class="num">When</th></tr>{recent_rows}</table></div>'
        if recent
        else '<p class="empty">Volunteer results will appear here.</p>'
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>fe70: does it tile a rectangle?</title><style>{STYLE}</style></head><body><main>
<header>{piece_svg(piece.cells)}<div><h1>Does fe70 tile a rectangle?</h1>
<p class="lead">fe70 is the 10-cell polyomino B(1.2^3.1^3) shown here, the last open case of Dahlke's gun
theorem. This is a volunteer search for a rectangle it tiles; each square on the map is one rectangle size,
checked by a complete computer search.</p>
</div></header>
{found_html}
<div class="stats">{stats_html}</div>
{join_html}
<section class="card"><h2>Rectangle sizes</h2>
<ul class="legend">{legend}</ul>
<div class="map">{"".join(parts)}<div class="tip" hidden></div></div>
<p class="note">{map_note(piece)}</p></section>
{gallery_html}
<div class="two">
<section class="card"><h2>Running now</h2>{running_html}</section>
<section class="card"><h2>Leaderboard</h2>{leader_html}</section>
</div>
<section class="card"><h2>Recent results</h2>{recent_html}</section>
{hunt_html}
<footer>Updated every 30 seconds. Raw data: <a href="/api/results">/api/results</a>.</footer>
</main><script>{TOOLTIP_JS}</script></body></html>"""
