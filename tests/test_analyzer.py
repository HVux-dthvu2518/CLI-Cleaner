"""Disk Analyzer: ranking, aggregation, partial scans and cache reuse."""

from __future__ import annotations

import asyncio
import csv
import os
from pathlib import Path

import pytest

from disk_explorer.analyzer import (
    AnalysisFilters,
    AnalyzerLimits,
    AnalyzerService,
    SnapshotStatus,
)
from disk_explorer.analyzer.aggregators import Counters, ExtensionAccumulator, TopFiles, TopFolders
from disk_explorer.analyzer.models import FileRecord, FolderRecord
from disk_explorer.analyzer.report_exporter import export_extensions, export_files, export_folders
from disk_explorer.cache.cache_service import CacheService
from disk_explorer.cache.database import Database
from disk_explorer.core.scanner import FileSystemScanner, ScanCancellation


def _record(name: str, size: int, directory: Path) -> FileRecord:
    path = directory / name
    return FileRecord(
        path=path,
        name=name,
        size=size,
        extension=Path(name).suffix.lower(),
        depth=0,
    )


def _folder(name: str, size: int | None, directory: Path, complete: bool = True) -> FolderRecord:
    return FolderRecord(
        path=directory / name,
        name=name,
        size=size,
        file_count=1,
        directory_count=1,
        is_complete=complete,
        depth=0,
    )


class CountingScanner:
    """Scanner double wrapping the real one so calls can be counted."""

    engine_name = "counting"

    def __init__(self) -> None:
        self.inner = FileSystemScanner()
        self.enumerations = 0
        self.size_calculations = 0

    def enumerate_directory(self, path, cancellation=None):
        self.enumerations += 1
        return self.inner.enumerate_directory(path, cancellation)

    def calculate_size(self, path, cancellation=None):
        self.size_calculations += 1
        return self.inner.calculate_size(path, cancellation)


@pytest.fixture()
def cache(tmp_path: Path) -> CacheService:
    service = CacheService(Database(tmp_path / "cache.sqlite3"), ttl_minutes=30)
    assert service.open()
    yield service
    service.close()


# -- M3.2 ranking ----------------------------------------------------------
def test_top_files_keeps_only_the_limit(tmp_path: Path) -> None:
    top = TopFiles(3)
    for index, size in enumerate([5, 500, 1, 90, 250, 12]):
        top.offer(_record(f"f{index}.bin", size, tmp_path))

    ranked = top.ranked()

    assert [record.size for record in ranked] == [500, 250, 90]
    assert top.offered == 6  # every candidate was considered
    assert top.kept == 3


def test_equal_sizes_order_by_path_for_stable_reports(tmp_path: Path) -> None:
    top = TopFiles(5)
    for name in ["delta.bin", "alpha.bin", "charlie.bin", "bravo.bin"]:
        top.offer(_record(name, 100, tmp_path))

    assert [record.name for record in top.ranked()] == [
        "alpha.bin",
        "bravo.bin",
        "charlie.bin",
        "delta.bin",
    ]


def test_ascending_order_is_supported(tmp_path: Path) -> None:
    top = TopFiles(3)
    for index, size in enumerate([7, 1, 4]):
        top.offer(_record(f"f{index}.bin", size, tmp_path))

    assert [record.size for record in top.ranked(descending=False)] == [1, 4, 7]


def test_folders_with_unknown_size_rank_below_measured_ones(tmp_path: Path) -> None:
    top = TopFolders(5)
    top.offer(_folder("unknown", None, tmp_path))
    top.offer(_folder("tiny", 1, tmp_path))
    top.offer(_folder("big", 999, tmp_path))

    ranked = top.ranked()

    assert [record.name for record in ranked] == ["big", "tiny", "unknown"]
    assert ranked[-1].size is None  # never coerced to zero


def test_top_n_rejects_a_zero_limit() -> None:
    with pytest.raises(ValueError):
        TopFiles(0)


# -- M3.3 extension statistics --------------------------------------------
def test_extension_aggregation_counts_and_sizes() -> None:
    accumulator = ExtensionAccumulator()
    accumulator.add(".mp4", 1000)
    accumulator.add(".mp4", 500)
    accumulator.add(".txt", 10)

    stats = {stat.extension: stat for stat in accumulator.ranked()}

    assert stats[".mp4"].file_count == 2
    assert stats[".mp4"].total_size == 1500
    assert stats[".mp4"].average_size == 750.0
    assert accumulator.total_size == 1510
    assert [stat.extension for stat in accumulator.ranked()] == [".mp4", ".txt"]


def test_files_without_extension_are_grouped() -> None:
    accumulator = ExtensionAccumulator()
    accumulator.add("", 10)
    accumulator.add("", 20)

    stats = accumulator.ranked()

    assert len(stats) == 1
    assert stats[0].extension == ""
    assert stats[0].file_count == 2


def test_extension_matching_is_case_insensitive() -> None:
    accumulator = ExtensionAccumulator()
    accumulator.add(".MP4", 10)
    accumulator.add(".mp4", 20)
    accumulator.add("Mp4", 30)

    stats = accumulator.ranked()

    assert len(stats) == 1 and stats[0].extension == ".mp4"
    assert stats[0].file_count == 3


def test_equal_extension_totals_order_alphabetically() -> None:
    accumulator = ExtensionAccumulator()
    accumulator.add(".zzz", 50)
    accumulator.add(".aaa", 50)

    assert [stat.extension for stat in accumulator.ranked()] == [".aaa", ".zzz"]


def test_counters_never_derive_totals_from_folder_sizes() -> None:
    """Folder totals are recursive; summing them would double count."""
    counters = Counters()
    counters.add_directory(0)
    counters.add_file(_record("a.bin", 100, Path(".")))
    counters.add_file(_record("b.bin", 50, Path(".")))

    assert counters.size == 150  # files only
    assert counters.files == 2


# -- analysis over a real tree --------------------------------------------
def _build_tree(root: Path) -> None:
    (root / "media").mkdir()
    (root / "media" / "clip.mp4").write_bytes(b"m" * 9000)
    (root / "media" / "poster.PNG").write_bytes(b"p" * 100)
    (root / "docs").mkdir()
    (root / "docs" / "report.pdf").write_bytes(b"r" * 500)
    (root / "docs" / "deep").mkdir()
    (root / "docs" / "deep" / "note.txt").write_bytes(b"n" * 20)
    (root / "README").write_bytes(b"x" * 7)  # no extension


@pytest.mark.asyncio
async def test_analysis_reports_files_folders_and_types(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)

    snapshot = await AnalyzerService().analyze(root)

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.total_files == 5
    assert snapshot.total_size == 9000 + 100 + 500 + 20 + 7
    assert [record.name for record in snapshot.largest_files[:2]] == ["clip.mp4", "report.pdf"]
    folder_names = [record.name for record in snapshot.largest_folders]
    assert folder_names[0] == "scope"  # root has the largest recursive total
    assert "media" in folder_names and "docs" in folder_names
    extensions = {stat.extension: stat.total_size for stat in snapshot.extensions.entries}
    assert extensions[".mp4"] == 9000
    assert extensions[""] == 7  # extension-less file is not dropped
    assert snapshot.storage is not None and snapshot.storage.drive_total


@pytest.mark.asyncio
async def test_folder_totals_are_not_summed_into_the_grand_total(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)

    snapshot = await AnalyzerService().analyze(root)

    measured_folders = sum(record.size or 0 for record in snapshot.largest_folders)
    assert measured_folders > snapshot.total_size  # proves totals are not double counted
    assert snapshot.total_size == sum(record.size for record in snapshot.largest_files)


@pytest.mark.asyncio
async def test_empty_directory_is_a_legitimate_zero(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    snapshot = await AnalyzerService().analyze(root)

    assert snapshot.status is SnapshotStatus.COMPLETE
    assert snapshot.total_files == 0
    assert snapshot.total_size == 0
    assert snapshot.largest_files == []
    assert snapshot.largest_folders[0].size == 0


@pytest.mark.asyncio
async def test_missing_root_fails_without_scanning(tmp_path: Path) -> None:
    snapshot = await AnalyzerService().analyze(tmp_path / "nope")

    assert snapshot.status is SnapshotStatus.FAILED
    assert snapshot.errors and "not a readable directory" in snapshot.errors[0]
    assert snapshot.largest_files == []


@pytest.mark.asyncio
async def test_permission_errors_make_the_report_partial(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    open_folder = root / "open"
    open_folder.mkdir()
    (open_folder / "seen.bin").write_bytes(b"s" * 40)
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "hidden.bin").write_bytes(b"h" * 10)
    try:
        os.chmod(blocked, 0o000)
    except OSError:
        pytest.skip("cannot change permissions here")
    try:
        snapshot = await AnalyzerService().analyze(root)
    finally:
        os.chmod(blocked, 0o755)

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert snapshot.errors
    assert snapshot.total_size == 40  # only what could be read
    folder_by_name = {record.name: record for record in snapshot.largest_folders}
    assert folder_by_name["open"].is_complete is True
    assert folder_by_name["blocked"].is_complete is False


@pytest.mark.asyncio
async def test_cancelled_analysis_reports_cancelled(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)
    cancellation = ScanCancellation()
    cancellation.set()

    snapshot = await AnalyzerService().analyze(root, cancellation=cancellation)

    assert snapshot.status is SnapshotStatus.CANCELLED
    assert snapshot.cancelled is True


@pytest.mark.asyncio
async def test_limits_mark_the_report_partial(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)

    snapshot = await AnalyzerService(limits=AnalyzerLimits(max_directories=1)).analyze(root)

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert any("stopped after" in problem for problem in snapshot.errors)


# -- filters ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_minimum_size_filter(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)

    snapshot = await AnalyzerService().analyze(root, filters=AnalysisFilters(min_size=100))

    assert all(record.size >= 100 for record in snapshot.largest_files)
    assert "README" not in [record.name for record in snapshot.largest_files]
    assert snapshot.total_size == 9000 + 100 + 500


@pytest.mark.asyncio
async def test_extension_filter_is_case_insensitive(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)

    snapshot = await AnalyzerService().analyze(root, filters=AnalysisFilters(extension="PNG"))

    assert [record.name for record in snapshot.largest_files] == ["poster.PNG"]


@pytest.mark.asyncio
async def test_max_size_filter(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)

    snapshot = await AnalyzerService().analyze(
        root, filters=AnalysisFilters(min_size=0, max_size=100)
    )

    assert {record.name for record in snapshot.largest_files} == {"poster.PNG", "note.txt", "README"}


# -- cache integration ----------------------------------------------------
@pytest.mark.asyncio
async def test_folder_sizes_are_reused_from_the_cache(tmp_path: Path, cache: CacheService) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)
    scanner = CountingScanner()
    service = AnalyzerService(scanner, cache)

    await service.analyze(root)
    first = scanner.size_calculations
    await service.analyze(root)

    assert first == 4  # root + media + docs + deep
    assert scanner.size_calculations == first  # second pass served from the cache


@pytest.mark.asyncio
async def test_force_refresh_ignores_cached_sizes(tmp_path: Path, cache: CacheService) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)
    scanner = CountingScanner()
    service = AnalyzerService(scanner, cache)

    await service.analyze(root)
    first = scanner.size_calculations
    await service.analyze(root, force_refresh=True)

    assert scanner.size_calculations == first * 2  # every folder rescanned


@pytest.mark.asyncio
async def test_partial_results_are_cached_and_stay_labelled(tmp_path: Path, cache: CacheService) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "hidden.bin").write_bytes(b"h" * 10)
    try:
        os.chmod(blocked, 0o000)
    except OSError:
        pytest.skip("cannot change permissions here")
    try:
        snapshot = await AnalyzerService(FileSystemScanner(), cache).analyze(root)
        cached = cache.cached_size(root)
    finally:
        os.chmod(blocked, 0o755)

    assert snapshot.status is SnapshotStatus.PARTIAL
    assert cached is not None  # the expensive case is cacheable even when partial
    assert cached.is_complete is False
    assert cached.errors


@pytest.mark.asyncio
async def test_progress_is_reported_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)
    seen: list[int] = []

    async def on_progress(progress) -> None:
        seen.append(progress.files_seen)

    await AnalyzerService(limits=AnalyzerLimits(progress_interval=0.0)).analyze(
        root, on_progress=on_progress
    )

    assert seen  # emitted at least once


# -- CSV export -----------------------------------------------------------
@pytest.mark.asyncio
async def test_csv_exports_are_utf8_with_status_rows(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    _build_tree(root)
    (root / "unicode-Ω-文件.txt").write_bytes(b"u" * 3)
    snapshot = await AnalyzerService().analyze(root)

    files_csv = export_files(snapshot, tmp_path / "out" / "files.csv")
    folders_csv = export_folders(snapshot, tmp_path / "out" / "folders.csv")
    types_csv = export_extensions(snapshot, tmp_path / "out" / "types.csv")

    with open(files_csv, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["rank", "name", "extension", "size_bytes", "folder", "path"]
    assert any("unicode-Ω-文件.txt" in row for row in rows)
    assert ["scan_status", "complete"] in rows

    with open(folders_csv, encoding="utf-8-sig", newline="") as handle:
        folder_rows = list(csv.reader(handle))
    assert folder_rows[0][:3] == ["rank", "name", "size_bytes"]
    assert any(row and row[0] == "total_files" for row in folder_rows)

    with open(types_csv, encoding="utf-8-sig", newline="") as handle:
        type_rows = list(csv.reader(handle))
    assert type_rows[0][0] == "extension"
    assert any(row and row[0] == "(none)" for row in type_rows)


@pytest.mark.asyncio
async def test_partial_export_warns_in_the_file(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "hidden.bin").write_bytes(b"h" * 5)
    try:
        os.chmod(blocked, 0o000)
    except OSError:
        pytest.skip("cannot change permissions here")
    try:
        snapshot = await AnalyzerService().analyze(root)
    finally:
        os.chmod(blocked, 0o755)
    assert snapshot.status is SnapshotStatus.PARTIAL

    path = export_files(snapshot, tmp_path / "files.csv")
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    assert ["scan_status", "partial"] in rows
    assert any(row and row[0] == "warning" and "partial" in row[1] for row in rows)


def test_unknown_folder_size_is_blank_in_csv_not_zero(tmp_path: Path) -> None:
    from disk_explorer.analyzer.models import AnalysisSnapshot

    snapshot = AnalysisSnapshot(
        root=tmp_path,
        status=SnapshotStatus.PARTIAL,
        largest_folders=[_folder("mystery", None, tmp_path, complete=False)],
    )

    path = export_folders(snapshot, tmp_path / "folders.csv")
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))

    mystery = next(row for row in rows if row and row[1] == "mystery")
    assert mystery[2] == ""  # blank, never "0"


# -- scale ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_large_synthetic_tree_stays_bounded(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    for folder_index in range(10):
        folder = root / f"f{folder_index}"
        folder.mkdir()
        for item in range(50):
            (folder / f"x{item}.bin").write_bytes(b"x" * (item + 1))

    snapshot = await AnalyzerService().analyze(root, top_files=25, top_folders=5)

    assert snapshot.total_files == 500
    assert len(snapshot.largest_files) == 25  # bounded output
    assert len(snapshot.largest_folders) == 5
    sizes = [record.size for record in snapshot.largest_files]
    assert sizes == sorted(sizes, reverse=True)
    assert snapshot.total_size == sum(range(1, 51)) * 10


def test_analysis_models_import_no_ui_or_sqlite() -> None:
    """The analyzer must stay usable without Textual (M4/M5 will reuse it)."""
    import ast

    source = (Path(__file__).resolve().parents[1] / "src" / "disk_explorer" / "analyzer").rglob("*.py")
    for path in source:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(name.startswith("textual") for name in imported), f"{path} imports Textual"
        assert not any(name.startswith("sqlite3") for name in imported), f"{path} imports sqlite3"


def test_analyzer_does_not_scan_on_construction(tmp_path: Path) -> None:
    """Building the service must not touch the filesystem."""
    scanner = CountingScanner()
    service = AnalyzerService(scanner)
    assert service.limits.max_directories > 0
    assert scanner.enumerations == 0 and scanner.size_calculations == 0
    assert not asyncio.iscoroutine(service)
