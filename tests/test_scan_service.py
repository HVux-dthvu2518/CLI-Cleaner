from pathlib import Path

import pytest

from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.services.scan_service import ScanService


@pytest.mark.asyncio
async def test_async_service_enumerates_then_updates_folder_size(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "data.bin").write_bytes(b"data")
    service = ScanService(max_workers=2)
    updates: list[tuple[str, int | None]] = []

    try:
        listing = await service.enumerate_directory(str(tmp_path))
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
