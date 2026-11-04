import os
from pathlib import Path
import threading

import pytest

from disk_explorer.core.scanner import FileSystemScanner, ScanCancellation
from disk_explorer.models.file_entry import ScanStatus
from disk_explorer.models.skip_reason import SkipReason


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


def test_pre_cancelled_enumeration_is_incomplete(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("test", encoding="utf-8")
    cancellation = ScanCancellation()
    cancellation.set()

    result = FileSystemScanner().enumerate_directory(tmp_path, cancellation)

    assert result.cancelled
    assert not result.is_complete
    assert result.entries == []


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
    assert "not traversed (symlink)" in listing.errors[0]


def test_missing_root_is_an_incomplete_result(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    listing = FileSystemScanner().enumerate_directory(missing)
    result = FileSystemScanner().calculate_size(missing)

    assert not listing.is_complete
    assert listing.errors
    assert not result.is_complete
    assert result.total_size == 0
    assert result.errors


def _make_symlink(link: Path, target: Path, is_directory: bool = True) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/environment cannot create symlinks")


def test_skipped_symlink_is_reported_with_a_reason(tmp_path: Path) -> None:
    """A link is excluded by design, and the result must say why."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "payload.bin").write_bytes(b"x" * 32)
    _make_symlink(tmp_path / "linked", target)

    result = FileSystemScanner().calculate_size(tmp_path)

    assert result.skipped_entries == 1
    assert [item.reason for item in result.skipped_details] == [SkipReason.SYMLINK]
    assert result.skipped_details[0].path == tmp_path / "linked"
    assert result.skipped_summary == "1 symlink(s)"
    # The link is not double counted, but the real directory still is.
    assert result.total_size == 32
    assert result.is_complete


def test_listing_explains_every_excluded_entry(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    _make_symlink(tmp_path / "linked_dir", target)
    _make_symlink(tmp_path / "linked_file.txt", target / "payload.bin", is_directory=False)
    (tmp_path / "plain.txt").write_text("abc", encoding="utf-8")

    listing = FileSystemScanner().enumerate_directory(tmp_path)

    assert listing.skipped_entries == 2
    reasons = {item.path.name: item.reason for item in listing.skipped_details}
    assert reasons == {"linked_dir": SkipReason.SYMLINK, "linked_file.txt": SkipReason.SYMLINK}
    assert listing.skipped_summary == "2 symlink(s)"
    # Ordinary files are never dropped from the listing.
    assert {entry.name for entry in listing.entries} >= {"plain.txt", "target"}


def test_directory_listing_counts_match_os_scandir(tmp_path: Path) -> None:
    """Guards against silent drops of ordinary files."""
    for index in range(25):
        (tmp_path / f"file{index:02d}.dat").write_bytes(b"y" * index)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.dat").write_bytes(b"z")

    listing = FileSystemScanner().enumerate_directory(tmp_path)

    assert len(listing.entries) == len(os.listdir(tmp_path)) == 26
    assert listing.scanned_entries == 26
    assert listing.skipped_entries == 0
    assert listing.is_complete


def test_recursive_scan_counts_matches_walk(tmp_path: Path) -> None:
    for index in range(5):
        folder = tmp_path / f"folder{index}"
        folder.mkdir()
        (folder / "a.bin").write_bytes(b"a" * (index + 1))
        (folder / "b.bin").write_bytes(b"b" * 3)

    result = FileSystemScanner().calculate_size(tmp_path)
    expected_files = sum(len(files) for _, _, files in os.walk(tmp_path))

    assert result.total_files == expected_files == 10
    assert result.total_size == sum(index + 1 + 3 for index in range(5))


def test_hidden_and_unusual_names_are_listed(tmp_path: Path) -> None:
    names = [".hidden", "name with spaces.txt", "unicode-Ω-文件.txt", "trailing."]
    for name in names:
        (tmp_path / name).write_bytes(b"q")

    listing = FileSystemScanner().enumerate_directory(tmp_path)

    assert {entry.name for entry in listing.entries} == set(names)


def test_extension_matches_pathlib_suffix(tmp_path: Path) -> None:
    names = [".gitignore", "archive.TAR.GZ", "noext", "double..txt", "trailing."]
    for name in names:
        (tmp_path / name).write_bytes(b"q")

    listing = FileSystemScanner().enumerate_directory(tmp_path)
    by_name = {entry.name: entry.extension for entry in listing.entries}

    assert by_name == {name: Path(name).suffix for name in names}


def test_permission_denied_is_an_error_not_a_silent_zero(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "secret.bin").write_bytes(b"s" * 128)
    (tmp_path / "visible.bin").write_bytes(b"v" * 4)
    try:
        os.chmod(blocked, 0o000)
    except OSError:
        pytest.skip("cannot change permissions here")
    try:
        result = FileSystemScanner().calculate_size(tmp_path)
    finally:
        os.chmod(blocked, 0o755)

    assert not result.is_complete
    assert result.errors and "blocked" in result.errors[0]
    assert result.total_size == 4  # never reported as a complete 0-byte scan


def test_non_regular_file_is_counted_as_skipped(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFO support")
    folder = tmp_path / "fifo"
    folder.mkdir()
    os.mkfifo(folder / "pipe")
    (folder / "real.bin").write_bytes(b"r" * 7)

    result = FileSystemScanner().calculate_size(folder)

    assert result.skipped_entries == 1
    assert [item.reason for item in result.skipped_details] == [SkipReason.NON_REGULAR_FILE]
    assert result.total_size == 7


def test_follow_links_opt_in_counts_the_link_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.bin").write_bytes(b"x" * 64)
    checked = tmp_path / "checked"
    checked.mkdir()
    _make_symlink(checked / "linked", outside)

    strict = FileSystemScanner().calculate_size(checked)
    opted_in = FileSystemScanner(follow_links=True).calculate_size(checked)

    assert strict.skipped_entries == 1
    assert strict.total_size == 0
    assert opted_in.skipped_entries == 0
    assert opted_in.total_size == 64
    assert opted_in.total_files == 1


def test_follow_links_does_not_loop_through_cycles(tmp_path: Path) -> None:
    folder = tmp_path / "loop"
    folder.mkdir()
    (folder / "data.bin").write_bytes(b"d" * 16)
    _make_symlink(folder / "self", folder)

    result = FileSystemScanner(follow_links=True).calculate_size(folder)

    assert result.total_size == 16
    assert result.total_files == 1
    assert [item.reason for item in result.skipped_details] == [SkipReason.DUPLICATE_DIRECTORY]


def test_link_root_reports_an_actionable_reason(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.bin").write_bytes(b"i" * 3)
    _make_symlink(tmp_path / "link", target)

    blocked = FileSystemScanner().calculate_size(tmp_path / "link")
    allowed = FileSystemScanner(follow_links=True).calculate_size(tmp_path / "link")

    assert not blocked.is_complete
    assert blocked.skipped_details[0].reason is SkipReason.SYMLINK
    assert "enable link traversal" in blocked.errors[0]
    assert allowed.is_complete
    assert allowed.total_size == 3
