"""Visual layer: bars, ring and treemap are honest about proportions.

The point of these tests is that a picture must never imply something the numbers
do not say: unknown sizes are not drawn as zero, boxes are mutually exclusive, and
whatever is not drawn is accounted for.
"""

from __future__ import annotations

from pathlib import Path

from disk_explorer.analyzer import AnalyzerService, SnapshotStatus, visuals
from disk_explorer.analyzer.models import (
    AnalysisSnapshot,
    ExtensionStat,
    ExtensionSummary,
    FolderRecord,
    StorageOverview,
)


def _snapshot(tmp_path: Path) -> AnalysisSnapshot:
    root = tmp_path / "scope"
    (root / "big").mkdir(parents=True)
    (root / "small").mkdir()
    (root / "big" / "movie.mp4").write_bytes(b"m" * 80_000)
    (root / "big" / "clip.mkv").write_bytes(b"c" * 20_000)
    (root / "small" / "notes.txt").write_bytes(b"n" * 100)
    (root / "loose.bin").write_bytes(b"b" * 4_000)
    return AnalyzerService().analyze_sync(root)


def test_bar_is_proportional_and_bounded() -> None:
    assert visuals.bar(0.0) == ""
    assert visuals.bar(1.0, 10) == "█" * 10
    assert visuals.bar(0.5, 10) == "█" * 5
    assert len(visuals.bar(2.0, 10)) == 10  # clamped, never overflows the column


def test_bar_chart_scales_to_the_largest_row() -> None:
    lines = visuals.bar_chart([("big", 1000, "1 KB"), ("small", 100, "100 B")], width=10)
    assert lines[0].endswith("1 KB")
    assert lines[0].count("█") == 10
    assert lines[1].count("█") == 1  # a tenth of the largest, not "full"


def test_treemap_boxes_are_mutually_exclusive_children(tmp_path: Path) -> None:
    """A folder and its parent must never be drawn side by side (double counting)."""
    snapshot = _snapshot(tmp_path)
    names = [record.name for record in snapshot.children]
    assert set(names) == {"big", "small"}
    assert "scope" not in names

    drawn = "\n".join(visuals.treemap(snapshot, width=40, rows=14))
    assert "big" in drawn and "small" in drawn
    assert "scope" not in drawn


def test_treemap_area_tracks_size(tmp_path: Path) -> None:
    """The 80 KB folder must claim visibly more cells than the 100 B one."""
    snapshot = _snapshot(tmp_path)
    drawing, _legend = visuals.treemap_parts(snapshot, width=50, rows=16, styles=visuals.STYLES)
    grid = "".join(drawing)
    tiles = {tile for tile in grid if tile != " "}
    counts = {tile: grid.count(tile) for tile in tiles}
    assert counts
    assert max(counts.values()) >= 3 * min(counts.values())


def test_treemap_keeps_a_tiny_folder_visible(tmp_path: Path) -> None:
    """A 99.9% folder must not round its neighbour out of existence."""
    root = tmp_path / "scope"
    (root / "huge").mkdir(parents=True)
    (root / "tiny").mkdir()
    (root / "huge" / "movie.mp4").write_bytes(b"m" * 80_000)
    (root / "tiny" / "note.txt").write_bytes(b"n" * 100)
    snapshot = AnalyzerService().analyze_sync(root)

    drawing, legend = visuals.treemap_parts(snapshot, width=40, rows=10)
    tiles = {tile for line in drawing for tile in line if tile != " "}
    assert len(tiles) == 2  # both folders claim cells
    assert any("tiny" in line for line in legend)


def test_treemap_skips_unknown_sizes_without_calling_them_zero(tmp_path: Path) -> None:
    snapshot = AnalysisSnapshot(
        root=tmp_path,
        status=SnapshotStatus.PARTIAL,
        children=[
            FolderRecord(
                path=tmp_path / "known",
                name="known",
                size=1_000,
                file_count=2,
                directory_count=1,
                is_complete=True,
                depth=0,
            ),
            FolderRecord(
                path=tmp_path / "unknown",
                name="unknown",
                size=None,
                file_count=0,
                directory_count=1,
                is_complete=False,
                depth=0,
            ),
        ],
    )
    drawn = "\n".join(visuals.treemap(snapshot, width=30, rows=8))
    assert "known" in drawn
    assert "unknown" not in drawn  # unknown is not an artist's zero


def test_donut_labels_every_slice_and_flags_incomplete_coverage(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    text = "\n".join(visuals.donut(snapshot))
    assert ".mp4" in text
    assert any(glyph in text for glyph in ("█", "▓", "▒"))
    assert "unknown" not in text

    partial = AnalysisSnapshot(
        root=tmp_path,
        status=SnapshotStatus.PARTIAL,
        extensions=ExtensionSummary(
            entries=(
                ExtensionStat(extension=".mp4", total_size=1000, file_count=1, average_size=1000.0),
            ),
            total_size=1000,
            file_count=1,
        ),
        errors=["denied"],
    )
    assert "partial" in "\n".join(visuals.donut(partial)).lower()


def test_empty_snapshot_says_so(tmp_path: Path) -> None:
    snapshot = AnalysisSnapshot(root=tmp_path, status=SnapshotStatus.FAILED)
    assert "nothing measured" in visuals.treemap(snapshot)[0].lower()
    assert "nothing measured" in visuals.donut(snapshot)[0].lower()


def test_dashboard_separates_drive_space_from_logical_size(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot.storage = StorageOverview(
        root=snapshot.root,
        drive_total=1_000_000,
        drive_used=400_000,
        drive_free=600_000,
        scanned_size=snapshot.total_size,
    )
    text = "\n".join(visuals.dashboard(snapshot, width=100))
    assert "Drive used" in text
    assert "Analysed" in text
    assert "Folders by size" in text
    assert "Largest folders" in text and "Largest files" in text


def test_visuals_do_not_import_textual() -> None:
    source = Path(visuals.__file__).read_text(encoding="utf-8")
    assert "textual" not in source
