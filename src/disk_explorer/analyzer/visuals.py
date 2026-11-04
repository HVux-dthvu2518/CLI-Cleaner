"""Text visualisations for analysis results: bars, a ring and a treemap.

Sizes are easier to judge as shapes than as numbers, and a terminal cannot show a
bitmap, so everything here renders to plain text/rich markup computed from the same
:class:`AnalysisSnapshot` the tables use. No third-party charting dependency and no
Textual import: this module is pure functions over the models.

Three rules, all tested:

* the *biggest* item always gets the biggest shape, so a top-heavy distribution
  still reads correctly;
* an unknown size is never drawn as zero;
* anything not drawn is accounted for ("+N smaller not drawn") instead of being
  silently cropped.
"""

from __future__ import annotations

import math
from typing import Iterable

from disk_explorer.analyzer.models import AnalysisSnapshot

#: Shade ramp for ring slices: densest = largest share.
_LEVELS = ("█", "▓", "▒", "░", "·")

_BAR_FULL = "█"
_BAR_HALF = "▌"

#: Rich colours cycled per box and per slice.
PALETTE = (
    "#ff5f5f",
    "#ffaf5f",
    "#f5e663",
    "#8fe36b",
    "#5fd7d7",
    "#5fafff",
    "#b387ff",
    "#ff8fd7",
)
STYLES = list(PALETTE)
STYLES_BY_GLYPH = {glyph: PALETTE[index % len(PALETTE)] for index, glyph in enumerate(_LEVELS)}

#: Fill characters, one per box, so neighbouring boxes stay distinguishable.
_TILES = "█▓▒░▚▞▛▜▙▟▖▗▘▝"


def format_size(size: int | None) -> str:
    """Compact human size, used by every visualisation."""
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def bar(ratio: float, width: int = 12) -> str:
    """A solid bar for ``ratio`` in ``0..1``, using half blocks for precision."""
    if width < 1:
        return ""
    ratio = min(1.0, max(0.0, ratio))
    halves = round(ratio * width * 2)
    return _BAR_FULL * (halves // 2) + (_BAR_HALF if halves % 2 else "")


def bar_chart(
    rows: Iterable[tuple[str, int, str]],
    width: int = 12,
    limit: int = 10,
) -> list[str]:
    """Render ``(label, value, suffix)`` rows as proportional bars.

    The widest bar always represents the largest value, so a top-heavy distribution
    still shows relative shape instead of every bar being full.
    """
    items = [(label, value, suffix) for label, value, suffix in rows if value > 0][:limit]
    if not items:
        return []
    largest = max(value for _, value, _ in items)
    label_width = min(22, max(len(label) for label, _, _ in items))
    lines = []
    for label, value, suffix in items:
        drawn = bar(value / largest if largest else 0.0, width)
        lines.append(f"{label[:label_width]:<{label_width}} {drawn:<{width}} {suffix}")
    return lines


def folder_bars(snapshot: AnalysisSnapshot, width: int = 14, limit: int = 10) -> list[str]:
    """Largest folders, as bars."""
    return bar_chart(
        [(record.name, record.size or 0, format_size(record.size)) for record in snapshot.largest_folders],
        width=width,
        limit=limit,
    )


def top_files_table(snapshot: AnalysisSnapshot, limit: int = 8) -> list[str]:
    """Largest files, as bars."""
    return bar_chart(
        [(record.name, record.size, format_size(record.size)) for record in snapshot.largest_files],
        width=16,
        limit=limit,
    )


# -- ring chart ------------------------------------------------------------
_DONUT_ROWS = 6
_DONUT_COLS = 15


def _ring_cells() -> list[tuple[int, int]]:
    """Character positions forming a ring, ordered clockwise from 12 o'clock.

    Clockwise ordering is what makes a slice read as a contiguous arc instead of a
    scatter of dots.
    """
    centre_x = (_DONUT_COLS - 1) / 2
    centre_y = (_DONUT_ROWS - 1) / 2
    cells: list[tuple[float, int, int]] = []
    for row in range(_DONUT_ROWS):
        for column in range(_DONUT_COLS):
            # Terminal cells are about twice as tall as wide: squash x to stay round.
            dx = (column - centre_x) / 2.0
            dy = row - centre_y
            if 0.85 <= math.hypot(dx, dy) <= 1.85:
                cells.append((math.atan2(dx, -dy) % (2 * math.pi), row, column))
    cells.sort()
    return [(row, column) for _angle, row, column in cells]


_RING_CELLS = _ring_cells()


def donut(
    snapshot: AnalysisSnapshot,
    slices: int = len(_LEVELS),
    styles: dict[str, str] | None = None,
) -> list[str]:
    """Ring chart of analysed bytes per extension, with a legend.

    Each slice occupies a contiguous arc proportional to its share of analysed
    bytes. Bytes no file accounts for are never folded into a slice: an incomplete
    scan says so in the legend instead.
    """
    stats = [stat for stat in snapshot.extensions.entries if stat.total_size > 0][:slices]
    total = sum(stat.total_size for stat in stats)
    if not stats or total == 0:
        return ["(nothing measured yet)"]

    grid = [[" "] * _DONUT_COLS for _ in range(_DONUT_ROWS)]
    glyphs = _LEVELS
    cursor = 0
    for index, stat in enumerate(stats):
        span = max(1, round(stat.total_size / total * len(_RING_CELLS)))
        for position in range(cursor, min(cursor + span, len(_RING_CELLS))):
            row, column = _RING_CELLS[position]
            grid[row][column] = glyphs[index]
        cursor += span

    lines = []
    for row in grid:
        raw = "".join(row).rstrip()
        if not raw or styles is None:
            lines.append(raw)
            continue
        lines.append(_paint_runs(raw, {glyph: styles[glyph] for glyph in glyphs if glyph in styles}))

    legend = []
    for index, stat in enumerate(stats):
        share = snapshot.extensions.share(stat) * 100
        swatch = glyphs[index]
        if styles and swatch in styles:
            swatch = f"[{styles[swatch]}]{swatch}[/]"
        legend.append(
            f"{swatch} {(stat.extension or '(none)')[:12]:<12} {share:5.1f}%  {format_size(stat.total_size)}"
        )
    remaining = len(snapshot.extensions.entries) - len(stats)
    if remaining > 0:
        rest = snapshot.extensions.total_size - total
        legend.append(f"  +{remaining} more types        {format_size(rest)}")
    if not snapshot.is_complete:
        legend.append(f"! {snapshot.status.label}: shares cover measured bytes only")
    return lines + [""] + legend


# -- treemap ---------------------------------------------------------------
def _worst_ratio(row: list[float], side: float) -> float:
    """Worst width/height ratio in a row of areas laid along ``side`` (Bruls et al.)."""
    total = sum(row)
    if total <= 0 or side <= 0:
        return float("inf")
    worst = 0.0
    for area in row:
        if area <= 0:
            continue
        ratio = max(side * side * area / (total * total), (total * total) / (side * side * area))
        worst = max(worst, ratio)
    return worst


def _squarify(
    boxes: list[tuple[str, int]],
    x: float,
    y: float,
    width: float,
    height: float,
    out: list[tuple[str, float, float, float, float]],
) -> None:
    """Squarified treemap: boxes stay as close to square as the data allows.

    A naive slice-and-dice split degenerates in a wide, short area - every box
    becomes a full-height stripe, which reads as a bar chart rather than a map.
    Squarifying keeps aspect ratios near 1, which is what makes a treemap legible.
    """
    if not boxes or width < 1 or height < 1:
        return
    total_value = sum(value for _label, value in boxes) or 1
    scale = (width * height) / total_value
    remaining = [(label, value * scale) for label, value in boxes]
    left, top, w, h = x, y, width, height

    while remaining:
        side = min(w, h)
        row = [remaining[0]]
        index = 1
        while index < len(remaining):
            current = _worst_ratio([area for _label, area in row], side)
            extended = _worst_ratio([area for _label, area in row + [remaining[index]]], side)
            if extended > current:
                break
            row.append(remaining[index])
            index += 1
        row_area = sum(area for _label, area in row)
        if row_area <= 0:
            break
        if w >= h:
            thickness = min(row_area / h if h else w, w)
            cursor = top
            for label, area in row:
                cell_h = area / thickness if thickness else h
                out.append((label, left, cursor, thickness, cell_h))
                cursor += cell_h
            left += thickness
            w -= thickness
        else:
            thickness = min(row_area / w if w else h, h)
            cursor = left
            for label, area in row:
                cell_w = area / thickness if thickness else w
                out.append((label, cursor, top, cell_w, thickness))
                cursor += cell_w
            top += thickness
            h -= thickness
        remaining = remaining[index:]
        if remaining and (w < 1 or h < 1):
            # No room for a proper strip: give the tail a shared row so it stays
            # visible (and its legend entry stays truthful).
            share = (w if w > 0 else width) / max(1, len(remaining))
            cursor = left
            for label, _area in remaining:
                out.append((label, cursor, top, share, max(1, h)))
                cursor += share
            remaining = []


def _paint_runs(raw: str, paint: dict[str, str]) -> str:
    """Wrap runs of identical characters in one style tag each.

    A tag per character would be tens of thousands of characters per redraw.
    """
    if not raw:
        return ""
    rendered = []
    run_character, run_length = raw[0], 1
    for character in raw[1:]:
        if character == run_character:
            run_length += 1
            continue
        rendered.append(_run(run_character, run_length, paint))
        run_character, run_length = character, 1
    rendered.append(_run(run_character, run_length, paint))
    return "".join(rendered)


def _run(character: str, length: int, paint: dict[str, str]) -> str:
    style = paint.get(character)
    if style is None:
        return character * length
    return f"[{style}]{character * length}[/]"


def treemap(
    snapshot: AnalysisSnapshot,
    width: int = 60,
    rows: int = 10,
    source: str = "folders",
    limit: int = 10,
    styles: list[str] | None = None,
) -> list[str]:
    """Treemap plus its legend, as one block (drawing first)."""
    drawing, legend = treemap_parts(snapshot, width, rows, source, limit, styles)
    return drawing + ([""] + legend if legend else [])


def treemap_parts(
    snapshot: AnalysisSnapshot,
    width: int = 60,
    rows: int = 10,
    source: str = "folders",
    limit: int = 10,
    styles: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Treemap drawing and legend separately, so a dashboard can place them side by side.

    Box area is proportional to bytes, so "which folder is eating the disk" is
    answered by shape rather than by reading a sorted list of numbers.
    """
    if source == "extensions":
        entries = [(stat.extension or "(none)", stat.total_size) for stat in snapshot.extensions.entries]
    elif snapshot.children:
        # The analysed folder's own children are mutually exclusive, so the boxes
        # tile the area honestly. ``largest_folders`` cannot be drawn this way: it
        # holds a folder *and* its ancestors, which would double count.
        entries = [(record.name, record.size or 0) for record in snapshot.children]
    else:
        entries = [(record.name, record.size or 0) for record in snapshot.largest_folders]
    entries = [(label, size) for label, size in entries if size > 0][:limit]
    if not entries:
        return ["(nothing measured yet)"], []

    width = max(8, width)
    rows = max(3, rows)
    # A cell is about twice as tall as wide, so a map wider than ~2.5x its height
    # has room only for full-width stripes. Capping the width keeps boxes square.
    width = min(width, max(8, int(rows * 2.5)))

    boxes: list[tuple[str, float, float, float, float]] = []
    _squarify(entries, 0, 0, width * 2, rows, boxes)

    owner = [[" "] * width for _ in range(rows)]
    legend: list[str] = []
    hidden = 0
    hidden_size = 0
    for index, (label, left, top, box_width, box_height) in enumerate(boxes):
        tile = _TILES[index % len(_TILES)]
        style = styles[index] if styles and index < len(styles) else None
        # Back to character cells (half of the horizontal scale used above), and at
        # least one cell each: a folder that is 99.9% of the total would otherwise
        # round its neighbours out of existence.
        column = min(int(left / 2), max(0, width - 1))
        last_column = min(width, max(column + 1, int(round((left + box_width) / 2))))
        row = min(int(top), max(0, rows - 1))
        last_row = min(rows, max(row + 1, int(round(top + box_height))))
        for line in range(row, last_row):
            for position in range(column, last_column):
                owner[line][position] = tile
        size = entries[index][1] if index < len(entries) else 0
        if last_column - column < 1:
            hidden += 1
            hidden_size += size
            continue
        painted = f"[{style}]{tile}[/]" if style else tile
        legend.append(f"{painted} {label[:24]:<24} {format_size(size)}")

    drawn_tiles = {character for line in owner for character in line if character != " "}
    paint: dict[str, str] = {}
    if styles:
        for index in range(len(boxes)):
            tile = _TILES[index % len(_TILES)]
            if tile in drawn_tiles:
                paint[tile] = styles[index] if index < len(styles) else PALETTE[index % len(PALETTE)]

    lines = []
    for line in owner:
        raw = "".join(line).rstrip()
        if raw:
            lines.append(_paint_runs(raw, paint) if paint else raw)
    while lines and not lines[-1]:
        lines.pop()
    if hidden:
        legend.append(f"  +{hidden} smaller ({format_size(hidden_size)}) not drawn")
    return lines, legend


# -- composition -----------------------------------------------------------
def _visible(line: str) -> str:
    """Strip rich markup so column widths are measured on what is drawn."""
    out, depth = [], 0
    for character in line:
        if character == "[":
            depth += 1
            continue
        if character == "]" and depth:
            depth -= 1
            continue
        if not depth:
            out.append(character)
    return "".join(out)


def _columns(blocks: list[list[str]], gap: int = 2) -> list[str]:
    """Lay rendered blocks side by side, padding each to its own visible width."""
    widths = [max((len(_visible(line)) for line in block), default=0) for block in blocks]
    height = max((len(block) for block in blocks), default=0)
    lines = []
    for row in range(height):
        cells = []
        for index, block in enumerate(blocks):
            text = block[row] if row < len(block) else ""
            cells.append(text + " " * max(0, widths[index] - len(_visible(text))))
        lines.append((" " * gap).join(cells).rstrip())
    return lines


def dashboard(snapshot: AnalysisSnapshot, width: int = 100) -> list[str]:
    """One screen that answers "what is big": complete map, ring, bars, rankings.

    The map keeps a compact footprint (a wide, short rectangle can only produce
    stripes) and puts its legend beside it, so the picture stays readable while the
    rankings underneath still name the big items.
    """
    width = max(40, width)
    # Physical aspect: twice as many columns as rows is a square map on screen,
    # because terminal cells are about twice as tall as they are wide.
    map_rows = max(8, min(14, width // 8))
    map_width = min(max(12, map_rows * 2), max(12, width // 3))
    drawing, map_legend = treemap_parts(snapshot, width=map_width, rows=map_rows, styles=STYLES)

    header: list[str] = []
    storage = snapshot.storage
    if storage is not None and storage.drive_total:
        header.append(
            f"Drive used  {bar(storage.drive_used_ratio, 24):<24} "
            f"{storage.drive_used_ratio * 100:5.1f}%  {format_size(storage.drive_used)}"
        )
        header.append(
            f"Analysed    {bar(storage.scanned_ratio, 24):<24} "
            f"{storage.scanned_ratio * 100:5.1f}%  {format_size(storage.scanned_size)} scanned"
        )
        header.append("")

    top = _columns(
        [
            ["[b]Folders by size[/b]   [dim]area = bytes[/dim]"] + drawing,
            [""] + map_legend,
            ["[b]File types[/b]"] + donut(snapshot, styles=STYLES_BY_GLYPH),
        ]
    )
    bottom = _columns(
        [
            ["[b]Largest folders[/b]"]
            + (folder_bars(snapshot, width=max(8, width // 4), limit=8) or ["(none)"]),
            ["[b]Largest files[/b]"] + (top_files_table(snapshot, limit=8) or ["(none)"]),
        ]
    )
    footer = []
    if not snapshot.is_complete:
        footer = [
            "",
            f"[!] {snapshot.status.label}: totals cover only what could be read. "
            "Unknown sizes are blank, never zero.",
        ]
    return header + top + [""] + bottom + footer
