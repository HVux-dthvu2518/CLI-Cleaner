import asyncio
from pathlib import Path
import threading
import time

import pytest

from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import DirectoryListing
from disk_explorer.services.scan_service import ScanService


@pytest.mark.asyncio
async def test_async_service_enumerates_then_updates_folder_size(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "data.bin").write_bytes(b"data")
    service = ScanService(max_workers=2)
    updates: list[tuple[str, int | None]] = []

    try:
        listing = await service.enumerate_directory(tmp_path)
        entry = listing.entries[0]
        assert entry.size is None
        await service.calculate_child_sizes(
            listing.entries,
            lambda updated, _result: updates.append((updated.name, updated.size)),
        )
    finally:
        service.close()

    assert entry.size == 4
    assert entry.scan_status is ScanStatus.COMPLETE
    assert updates == [("folder", 4)]


@pytest.mark.asyncio
async def test_enumeration_task_cancellation_reaches_worker(tmp_path: Path) -> None:
    class WaitingScanner:
        engine_name = "python-test-double"

        def __init__(self) -> None:
            self.finished = threading.Event()
            self.cancellation = None

        def enumerate_directory(self, path, cancellation):
            self.cancellation = cancellation
            while not cancellation.is_set():
                time.sleep(0.001)
            self.finished.set()
            return DirectoryListing(root=Path(path), is_complete=False, cancelled=True)

        def calculate_size(self, path, cancellation):
            raise AssertionError("not used")

    scanner = WaitingScanner()
    service = ScanService(max_workers=1, scanner=scanner)
    task = asyncio.create_task(service.enumerate_directory(tmp_path))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert await asyncio.to_thread(scanner.finished.wait, 1)
        assert scanner.cancellation.is_set()
    finally:
        service.close()


@pytest.mark.asyncio
async def test_service_rejects_zero_workers() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ScanService(max_workers=0)


@pytest.mark.asyncio
async def test_service_reports_missing_directory_as_partial(tmp_path: Path) -> None:
    entry = FileEntry(
        path=tmp_path / "gone",
        name="gone",
        is_directory=True,
        size=None,
        modified_ns=None,
        extension="",
        scan_status=ScanStatus.PENDING,
    )
    service = ScanService(max_workers=1)
    try:
        await service.calculate_child_sizes([entry])
    finally:
        service.close()
    assert entry.size is None
    assert entry.scan_status is ScanStatus.PARTIAL
