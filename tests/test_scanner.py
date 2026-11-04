from pathlib import Path
import threading

from disk_explorer.core.scanner import FileSystemScanner
from disk_explorer.models.file_entry import ScanStatus


def test_enumeration_is_immediate_and_directory_sizes_are_pending(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "nested.bin").write_bytes(b"123456")
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")

    listing = FileSystemScanner().enumerate_directory(tmp_path)
    entries = {entry.name: entry for entry in listing.entries}

    assert listing.is_complete
    assert listing.scanned_entries == 2
    assert entries["folder"].is_directory
    assert entries["folder"].size is None
    assert entries["folder"].scan_status is ScanStatus.PENDING
    assert entries["readme.txt"].size == 2
    assert entries["readme.txt"].scan_status is ScanStatus.COMPLETE


def test_recursive_size_counts_regular_files_and_directories(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / "root.dat").write_bytes(b"12345")
    (nested / "child.dat").write_bytes(b"1234567")

    result = FileSystemScanner().calculate_size(tmp_path)

    assert result.is_complete
    assert result.total_size == 12
    assert result.total_files == 2
    assert result.total_directories == 3
    assert result.skipped_entries == 0


def test_symlink_is_listed_but_not_traversed_or_counted(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "large.bin").write_bytes(b"x" * 4096)
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return

    listing = FileSystemScanner().enumerate_directory(tmp_path)
    link_entry = next(entry for entry in listing.entries if entry.name == "link")
    result = FileSystemScanner().calculate_size(tmp_path)

    assert link_entry.is_symlink
    assert not link_entry.is_directory
    assert result.total_size == 4096
    assert result.total_directories == 2
    assert result.skipped_entries == 1


def test_pre_cancelled_scan_is_incomplete(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("test", encoding="utf-8")
    cancellation = threading.Event()
    cancellation.set()

    result = FileSystemScanner().calculate_size(tmp_path, cancellation)

    assert result.cancelled
    assert not result.is_complete
    assert result.total_size == 0


def test_symlink_root_is_not_enumerated(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "private.txt").write_text("secret", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return

    listing = FileSystemScanner().enumerate_directory(link)

    assert not listing.is_complete
    assert not listing.entries
    assert "not a traversable directory" in listing.errors[0]


def test_missing_root_is_an_incomplete_result(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    listing = FileSystemScanner().enumerate_directory(missing)
    result = FileSystemScanner().calculate_size(missing)

    assert not listing.is_complete
    assert listing.errors
    assert not result.is_complete
    assert result.total_size == 0
    assert result.errors
