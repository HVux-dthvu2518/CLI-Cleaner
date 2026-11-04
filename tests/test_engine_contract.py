"""The Rust/Python engine contract, checked without requiring a Rust toolchain.

The compiled extension cannot be built everywhere (it needs a Rust toolchain and
a native linker), so these tests do two things:

* they always validate the pure-Python engine as the reference behaviour for the
  contract below, and
* they additionally compare the live Rust engine against that reference when
  ``disk_explorer._rust_scanner`` imports successfully.

Shared contract (both engines, exposed through ``FileSystemScanner``):

``enumerate_directory(path, token, follow_links=False, reparse_mounts=False)``
    -> ``(entries, scanned, errors, is_complete, cancelled, skip_records)``
    where each entry is
    ``(name, is_directory, size|None, modified_ns|None, extension,
       is_symlink, is_reparse_point)``
``calculate_size(path, token, follow_links=False, reparse_mounts=False)``
    -> ``(total_size, total_files, total_directories, scanned, errors,
        is_complete, cancelled, skip_records)``
``skip_records`` is a list of ``(path, reason)`` pairs whose reason strings are
``SkipReason`` values. Neither engine may follow links unless the caller opts in.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from disk_explorer.core import scanner as scanner_module
from disk_explorer.core.scanner import FileSystemScanner
from disk_explorer.models.skip_reason import SkipReason

_RUST = scanner_module._rust_scanner
needs_rust = pytest.mark.skipif(_RUST is None, reason="Rust extension is not built in this environment")


def _build_fixture(root: Path) -> None:
    """A tree that exercises files, nesting, a link, an empty dir and odd names."""
    (root / "docs").mkdir()
    (root / "docs" / "readme.md").write_bytes(b"r" * 10)
    (root / "docs" / "deep").mkdir()
    (root / "docs" / "deep" / "note.txt").write_bytes(b"n" * 5)
    (root / "empty").mkdir()
    (root / "data.BIN").write_bytes(b"d" * 7)
    (root / ".hidden").write_bytes(b"h" * 3)
    (root / "name with spaces.txt").write_bytes(b"s" * 2)
    try:
        (root / "linked").symlink_to(root / "docs", target_is_directory=True)
    except (OSError, NotImplementedError):
        pass


def _reference_snapshot(scanner: FileSystemScanner, root: Path) -> dict[str, object]:
    listing = scanner.enumerate_directory(root)
    result = scanner.calculate_size(root)
    return {
        "names": sorted(entry.name for entry in listing.entries),
        "listing_flags": sorted(
            (entry.name, entry.is_directory, entry.is_symlink, entry.is_reparse_point)
            for entry in listing.entries
        ),
        "listing_sizes": sorted((entry.name, entry.size) for entry in listing.entries),
        "listing_scanned": listing.scanned_entries,
        "listing_skips": sorted((item.path.name, item.reason) for item in listing.skipped_details),
        "listing_errors": len(listing.errors),
        "total_size": result.total_size,
        "total_files": result.total_files,
        "total_directories": result.total_directories,
        "skips": sorted((item.path.name, item.reason) for item in result.skipped_details),
        "errors": len(result.errors),
        "is_complete": result.is_complete,
        "cancelled": result.cancelled,
    }


def test_python_engine_is_the_reference_implementation(tmp_path: Path) -> None:
    _build_fixture(tmp_path)

    snapshot = _reference_snapshot(FileSystemScanner(), tmp_path)

    assert snapshot["names"] == sorted(os.listdir(tmp_path))
    assert snapshot["total_size"] == 27
    assert snapshot["total_files"] == 5
    assert snapshot["is_complete"] is True
    if any("linked" == name for name, *_ in snapshot["listing_skips"]):
        assert snapshot["skips"] == [("linked", SkipReason.SYMLINK)]


@needs_rust
def test_rust_engine_matches_the_documented_raw_shape(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    token = _RUST.CancellationToken()

    listing = _RUST.enumerate_directory(str(tmp_path), token)
    size = _RUST.calculate_size(str(tmp_path), token)

    assert len(listing) == 6
    entries, scanned, errors, complete, cancelled, skip_records = listing
    assert isinstance(scanned, int) and isinstance(errors, list)
    assert isinstance(complete, bool) and isinstance(cancelled, bool)
    for entry in entries:
        name, is_directory, entry_size, modified_ns, extension, is_symlink, is_reparse = entry
        assert isinstance(name, str)
        assert isinstance(is_directory, bool)
        assert entry_size is None or isinstance(entry_size, int)
        assert modified_ns is None or isinstance(modified_ns, int)
        assert isinstance(extension, str)
        assert isinstance(is_symlink, bool) and isinstance(is_reparse, bool)
    for path, reason in skip_records:
        assert isinstance(path, str)
        SkipReason(reason)  # must be a reason the Python model understands

    assert len(size) == 8
    assert isinstance(size[0], int) and isinstance(size[7], list)


@needs_rust
def test_rust_and_python_engines_agree(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    rust = FileSystemScanner()
    reference = FileSystemScanner()
    assert rust.engine_name == "rust"

    rust_snapshot = _reference_snapshot(rust, tmp_path)
    python_snapshot = _reference_snapshot_python(reference, tmp_path)

    assert rust_snapshot == python_snapshot


def _reference_snapshot_python(scanner: FileSystemScanner, root: Path) -> dict[str, object]:
    """Snapshot produced explicitly by the Python fallback, whatever is installed."""
    listing = scanner._enumerate_python(root, scanner_module.ScanCancellation())
    result = scanner._calculate_size_python(root, scanner_module.ScanCancellation())
    return {
        "names": sorted(entry.name for entry in listing.entries),
        "listing_flags": sorted(
            (entry.name, entry.is_directory, entry.is_symlink, entry.is_reparse_point)
            for entry in listing.entries
        ),
        "listing_sizes": sorted((entry.name, entry.size) for entry in listing.entries),
        "listing_scanned": listing.scanned_entries,
        "listing_skips": sorted((item.path.name, item.reason) for item in listing.skipped_details),
        "listing_errors": len(listing.errors),
        "total_size": result.total_size,
        "total_files": result.total_files,
        "total_directories": result.total_directories,
        "skips": sorted((item.path.name, item.reason) for item in result.skipped_details),
        "errors": len(result.errors),
        "is_complete": result.is_complete,
        "cancelled": result.cancelled,
    }
