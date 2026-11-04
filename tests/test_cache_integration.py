"""Cache behaviour as seen through ScanService and the running UI."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from disk_explorer.application import DiskExplorerApp
from disk_explorer.cache.cache_service import CacheService
from disk_explorer.cache.database import Database
from disk_explorer.models.file_entry import ScanStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult
from disk_explorer.services.scan_service import ScanService, display_size


class CountingScanner:
    """Scanner double that counts real filesystem work."""

    engine_name = "test-double"

    def __init__(self) -> None:
        self.enumerations = 0
        self.size_calculations = 0

    def enumerate_directory(self, path, cancellation):
        self.enumerations += 1
        root = Path(path)
        entries = []
        from disk_explorer.models.file_entry import FileEntry

        for child in sorted(root.iterdir()):
            entries.append(
                FileEntry(
                    path=child,
                    name=child.name,
                    is_directory=child.is_dir(),
                    size=None if child.is_dir() else child.stat().st_size,
                    modified_ns=child.stat().st_mtime_ns,
                    extension=child.suffix,
                    scan_status=ScanStatus.PENDING if child.is_dir() else ScanStatus.COMPLETE,
                )
            )
        return DirectoryListing(root=root, entries=entries, scanned_entries=len(entries))

    def calculate_size(self, path, cancellation):
        self.size_calculations += 1
        root = Path(path)
        total = 0
        files = 0
        for directory, _dirs, names in os.walk(root):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(directory, name))
                    files += 1
                except OSError:
                    continue
        return ScanResult(root=root, total_size=total, total_files=files, total_directories=1)


@pytest.fixture()
def cache(tmp_path: Path) -> CacheService:
    service = CacheService(Database(tmp_path / "cache" / "scan.sqlite3"), ttl_minutes=30)
    assert service.open()
    yield service
    service.close()


def _tree(root: Path) -> Path:
    folder = root / "data"
    folder.mkdir()
    (folder / "one.bin").write_bytes(b"1" * 10)
    (folder / "two.bin").write_bytes(b"2" * 20)
    return folder


@pytest.mark.asyncio
async def test_second_enumeration_is_served_from_the_cache(tmp_path: Path, cache: CacheService) -> None:
    folder = _tree(tmp_path)
    scanner = CountingScanner()
    service = ScanService(max_workers=2, scanner=scanner, cache=cache)
    try:
        first = await service.enumerate_directory(folder)
        second = await service.enumerate_directory(folder)
    finally:
        service.close()

    assert scanner.enumerations == 1  # the filesystem was read once
    assert first.from_cache is False
    assert second.from_cache is True
    assert [entry.name for entry in second.entries] == ["one.bin", "two.bin"]
    assert second.skipped_details == first.skipped_details


@pytest.mark.asyncio
async def test_child_sizes_are_reused_without_rescanning(tmp_path: Path, cache: CacheService) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _tree(workspace)  # scan the parent so "data" is the only child folder
    folder = workspace
    scanner = CountingScanner()
    service = ScanService(max_workers=2, scanner=scanner, cache=cache)
    try:
        listing = await service.enumerate_directory(folder)
        await service.calculate_child_sizes(listing.entries)
        assert scanner.size_calculations == 1

        entries = (await service.enumerate_directory(folder)).entries
        await service.calculate_child_sizes(entries)
    finally:
        service.close()

    data_entry = next(entry for entry in entries if entry.name == "data")
    assert data_entry.size == 30
    assert data_entry.scan_status is ScanStatus.CACHED
    assert scanner.size_calculations == 1  # served from cache


@pytest.mark.asyncio
async def test_manual_refresh_forces_a_fresh_scan(tmp_path: Path, cache: CacheService) -> None:
    folder = _tree(tmp_path)
    scanner = CountingScanner()
    service = ScanService(max_workers=2, scanner=scanner, cache=cache)
    try:
        await service.enumerate_directory(folder)
        refreshed = await service.enumerate_directory(folder, refresh=True)
        await service.calculate_directory_size(folder)
        refreshed_size = await service.calculate_directory_size(folder, refresh=True)
    finally:
        service.close()

    assert refreshed.from_cache is False
    assert scanner.enumerations == 2
    assert refreshed_size is not None
    assert scanner.size_calculations == 2


@pytest.mark.asyncio
async def test_cancelled_scan_is_never_cached(tmp_path: Path, cache: CacheService) -> None:
    folder = _tree(tmp_path)

    class CancelledScanner(CountingScanner):
        def calculate_size(self, path, cancellation):
            cancellation.set()
            return ScanResult(root=Path(path), is_complete=False, cancelled=True)

    service = ScanService(max_workers=1, scanner=CancelledScanner(), cache=cache)
    try:
        result = await service.calculate_directory_size(folder)
        assert result.cancelled
        assert cache.cached_size(folder) is None
    finally:
        service.close()


@pytest.mark.asyncio
async def test_partial_scan_is_cached_but_labelled_partial(
    tmp_path: Path, cache: CacheService
) -> None:
    """Partial totals are cached (they are the expensive case) and stay partial."""
    folder = _tree(tmp_path)

    class PartialScanner(CountingScanner):
        def calculate_size(self, path, cancellation):
            return ScanResult(
                root=Path(path),
                is_complete=False,
                total_size=10,
                errors=[f"{path}: Permission denied"],
            )

    service = ScanService(max_workers=1, scanner=PartialScanner(), cache=cache)
    try:
        fresh = await service.calculate_directory_size(folder)
        cached = await service.calculate_directory_size(folder)
    finally:
        service.close()

    assert fresh.is_complete is False
    assert cached is not None
    assert cached.from_cache is True
    assert cached.is_complete is False
    assert cached.total_size == 10
    assert cached.errors == [f"{folder}: Permission denied"]


@pytest.mark.asyncio
async def test_partial_scan_ui_label_says_cached_partial(tmp_path: Path, cache: CacheService) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _tree(workspace)

    class PartialScanner(CountingScanner):
        def calculate_size(self, path, cancellation):
            return ScanResult(
                root=Path(path),
                is_complete=False,
                total_size=30,
                total_files=2,
                total_directories=1,
                scanned_entries=3,
                errors=[f"{path / 'blocked'}: Access is denied"],
            )

    service = ScanService(max_workers=1, scanner=PartialScanner(), cache=cache)
    try:
        listing = await service.enumerate_directory(workspace)
        await service.calculate_child_sizes(listing.entries)
        second = (await service.enumerate_directory(workspace)).entries
        await service.calculate_child_sizes(second)
    finally:
        service.close()

    data_entry = next(entry for entry in second if entry.name == "data")
    assert data_entry.scan_status is ScanStatus.CACHED_PARTIAL
    assert data_entry.size == 30


@pytest.mark.asyncio
async def test_cancelling_the_task_leaves_no_cache_row(tmp_path: Path, cache: CacheService) -> None:
    folder = _tree(tmp_path)

    class SlowScanner(CountingScanner):
        def enumerate_directory(self, path, cancellation):
            while not cancellation.is_set():
                time.sleep(0.001)
            return DirectoryListing(root=Path(path), is_complete=False, cancelled=True)

    service = ScanService(max_workers=1, scanner=SlowScanner(), cache=cache)
    task = asyncio.create_task(service.enumerate_directory(folder))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service.close()

    assert cache.repository.counts()["listings"] == 0


@pytest.mark.asyncio
async def test_cache_survives_an_application_restart(tmp_path: Path) -> None:
    folder = _tree(tmp_path)
    database_path = tmp_path / "cache" / "scan.sqlite3"

    first_session = CacheService(Database(database_path), ttl_minutes=30)
    assert first_session.open()
    service = ScanService(max_workers=1, cache=first_session)
    try:
        await service.enumerate_directory(folder)
    finally:
        service.close()

    second_session = CacheService(Database(database_path), ttl_minutes=30)
    assert second_session.open()
    try:
        cached = second_session.cached_listing(folder)
    finally:
        second_session.close()

    assert cached is not None
    assert sorted(entry.name for entry in cached.entries) == ["one.bin", "two.bin"]


@pytest.mark.asyncio
async def test_service_without_cache_behaves_as_before(tmp_path: Path) -> None:
    folder = _tree(tmp_path)
    scanner = CountingScanner()
    service = ScanService(max_workers=1, scanner=scanner)
    try:
        assert service.cache_enabled is False
        await service.enumerate_directory(folder)
        await service.enumerate_directory(folder)
        assert service.invalidate_cache(folder) == 0
    finally:
        service.close()

    assert scanner.enumerations == 2  # never served from a cache that does not exist


# -- UI integration --------------------------------------------------------
@pytest.mark.asyncio
async def test_ui_labels_a_cached_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "userdata"))
    folder = _tree(tmp_path)
    app = DiskExplorerApp()
    app.current_path = folder

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.cache_service is not None
        # Cache the listing behind the UI's back, then navigate again.
        app.cache_service.store_listing(await app.scan_service.enumerate_directory(folder, refresh=False))
        app._navigate_to(folder)
        await pilot.pause(0.3)

        assert app.query_one("#file-table", DataTable).row_count == 2
        assert app._entries[0].scan_status in {ScanStatus.CACHED, ScanStatus.COMPLETE, ScanStatus.SCANNING}


@pytest.mark.asyncio
async def test_ui_refresh_after_a_file_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "userdata"))
    folder = _tree(tmp_path)
    app = DiskExplorerApp()
    app.current_path = folder

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        (folder / "three.bin").write_bytes(b"3" * 5)
        stat = folder.stat()
        os.utime(folder, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        await pilot.press("r")  # manual refresh
        await pilot.pause(0.4)

        names = [entry.name for entry in app._entries]
        assert names == ["one.bin", "three.bin", "two.bin"]
        assert "3 folders" not in str(app.query_one("#listing-summary", Static).renderable)


# -- child-mtime cross-check ----------------------------------------------
@pytest.mark.asyncio
async def test_child_listing_change_makes_a_size_row_stale(tmp_path: Path, cache: CacheService) -> None:
    """A size row is distrusted when a listing written with it has changed.

    A directory's own mtime cannot see changes further down the tree, so cached
    child listings written after the size row provide the extra evidence. This is
    what ``cross_check_children`` adds on top of the TTL.
    """
    data = _tree(tmp_path)
    nested = data / "nested"
    nested.mkdir()
    (nested / "deep.bin").write_bytes(b"d" * 4)
    service = ScanService(max_workers=1, scanner=CountingScanner(), cache=cache)
    try:
        await service.calculate_directory_size(data)  # size row for "data"
        await service.enumerate_directory(nested)  # listing row for "data/nested"
        await service.enumerate_directory(data)

        # A deep change: "data" itself keeps its mtime, so only the recorded child
        # listing can reveal it.
        (nested / "added.bin").write_bytes(b"a")
        stat = nested.stat()
        os.utime(nested, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        assert cache.cached_size(data) is not None  # TTL alone still accepts it
        assert cache.cached_size(data, cross_check_children=True) is None
        assert "changed" in cache.stats.last_miss_reason
    finally:
        service.close()


@pytest.mark.asyncio
async def test_unmeasurable_partial_scan_keeps_an_unknown_size(tmp_path: Path) -> None:
    """A partial scan that measured nothing must not become a misleading "0 B"."""
    folder = _tree(tmp_path)

    class NothingMeasured(CountingScanner):
        def calculate_size(self, path, cancellation):
            return ScanResult(
                root=Path(path),
                is_complete=False,
                total_size=0,
                scanned_entries=0,
                errors=[f"{path}: Access is denied"],
            )

    service = ScanService(max_workers=1, scanner=NothingMeasured())
    try:
        result = await service.calculate_directory_size(folder)
    finally:
        service.close()

    assert display_size(result) is None

    complete_empty = ScanResult(root=folder, total_size=0, is_complete=True)
    assert display_size(complete_empty) == 0  # a real empty folder is 0 B
