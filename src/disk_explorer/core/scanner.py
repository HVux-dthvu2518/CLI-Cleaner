"""Fast enumeration and recursive-size scanning with a Rust acceleration path.

The extension releases Python's GIL during filesystem traversal. The Python
implementation remains a compatible fallback for source runs without a built
extension and for callers supplying their own threading.Event.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import Any

from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult

# Windows marks junctions and other reparse points with this file attribute.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

try:
    from disk_explorer import _rust_scanner
except (ImportError, OSError):  # Source-tree development can use the pure-Python engine.
    _rust_scanner = None


class ScanCancellation:
    """Cancellation handle shared by Python fallback and optional Rust worker."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._native = _rust_scanner.CancellationToken() if _rust_scanner is not None else None

    def set(self) -> None:
        self._event.set()
        if self._native is not None:
            self._native.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def native_token(self) -> Any | None:
        return self._native


class FileSystemScanner:
    """Filesystem-only scanner; it has no UI or persistence dependencies.

    Directory sizes are logical byte totals (`st_size`). Links/reparse points
    are never traversed. Inaccessible or changing entries make results partial.
    """

    @property
    def engine_name(self) -> str:
        return "rust" if _rust_scanner is not None else "python"

    def enumerate_directory(
        self,
        path: str | Path,
        cancel_event: threading.Event | ScanCancellation | None = None,
    ) -> DirectoryListing:
        """List immediate children without waiting for recursive folder sizes."""
        root = Path(path)
        cancellation = cancel_event or ScanCancellation()
        if _rust_scanner is not None and isinstance(cancellation, ScanCancellation):
            native_token = cancellation.native_token
            if native_token is not None:
                entries, scanned, skipped, errors, complete, cancelled = _rust_scanner.enumerate_directory(
                    str(root), native_token
                )
                return self._listing_from_rust(root, entries, scanned, skipped, errors, complete, cancelled)
        return self._enumerate_python(root, cancellation)

    def _listing_from_rust(
        self,
        root: Path,
        entries: list[tuple[Any, ...]],
        scanned: int,
        skipped: int,
        errors: list[str],
        complete: bool,
        cancelled: bool,
    ) -> DirectoryListing:
        return DirectoryListing(
            root=root,
            entries=[
                FileEntry(
                    path=root / name,
                    name=name,
                    is_directory=is_directory,
                    size=size,
                    modified_ns=modified_ns,
                    extension=extension,
                    scan_status=ScanStatus.PENDING if is_directory else ScanStatus.COMPLETE,
                    is_symlink=is_symlink,
                    is_reparse_point=is_reparse,
                )
                for name, is_directory, size, modified_ns, extension, is_symlink, is_reparse in entries
            ],
            scanned_entries=scanned,
            skipped_entries=skipped,
            errors=errors,
            is_complete=complete,
            cancelled=cancelled,
        )

    def calculate_size(
        self,
        path: str | Path,
        cancel_event: threading.Event | ScanCancellation | None = None,
    ) -> ScanResult:
        """Calculate a directory's logical size, dispatching to Rust if built."""
        root = Path(path)
        cancellation = cancel_event or ScanCancellation()
        if _rust_scanner is not None and isinstance(cancellation, ScanCancellation):
            native_token = cancellation.native_token
            if native_token is not None:
                (
                    total_size,
                    total_files,
                    total_directories,
                    scanned_entries,
                    skipped_entries,
                    errors,
                    is_complete,
                    cancelled,
                ) = _rust_scanner.calculate_size(str(root), native_token)
                return ScanResult(
                    root=root,
                    total_size=total_size,
                    total_files=total_files,
                    total_directories=total_directories,
                    scanned_entries=scanned_entries,
                    skipped_entries=skipped_entries,
                    errors=errors,
                    is_complete=is_complete,
                    cancelled=cancelled,
                )
        return self._calculate_size_python(root, cancellation)

    def _enumerate_python(
        self,
        root: Path,
        cancellation: threading.Event | ScanCancellation,
    ) -> DirectoryListing:
        result = DirectoryListing(root=root)
        if cancellation.is_set():
            result.is_complete = False
            result.cancelled = True
            return result
        try:
            root_stat = root.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or _is_reparse_point(root_stat)
            ):
                result.is_complete = False
                result.skipped_entries = 1
                result.errors.append(f"{root}: not a traversable directory")
                return result
            with os.scandir(root) as children:
                for child in children:
                    if cancellation.is_set():
                        result.is_complete = False
                        result.cancelled = True
                        break
                    child_path = Path(child.path)
                    result.scanned_entries += 1
                    try:
                        item_stat = child.stat(follow_symlinks=False)
                        is_symlink = child.is_symlink()
                        is_reparse = _is_reparse_point(item_stat)
                        is_directory = stat.S_ISDIR(item_stat.st_mode) and not (is_symlink or is_reparse)
                        result.entries.append(
                            FileEntry(
                                path=child_path,
                                name=child.name,
                                is_directory=is_directory,
                                size=None if is_directory else item_stat.st_size,
                                modified_ns=getattr(item_stat, "st_mtime_ns", None),
                                extension="" if is_directory else child_path.suffix.lower(),
                                scan_status=ScanStatus.PENDING if is_directory else ScanStatus.COMPLETE,
                                is_symlink=is_symlink,
                                is_reparse_point=is_reparse,
                            )
                        )
                    except OSError as error:
                        result.skipped_entries += 1
                        result.is_complete = False
                        result.errors.append(_error_message(child_path, error))
        except OSError as error:
            result.is_complete = False
            result.skipped_entries += 1
            result.errors.append(_error_message(root, error))
        return result

    def _calculate_size_python(
        self,
        root: Path,
        cancellation: threading.Event | ScanCancellation,
    ) -> ScanResult:
        result = ScanResult(root=root)
        try:
            root_stat = root.stat(follow_symlinks=False)
        except OSError as error:
            result.is_complete = False
            result.skipped_entries = 1
            result.errors.append(_error_message(root, error))
            return result

        if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse_point(root_stat):
            result.is_complete = False
            result.skipped_entries = 1
            result.errors.append(f"{root}: not a traversable directory")
            return result

        visited: set[tuple[int, int]] = set()

        def identity(item_stat: os.stat_result) -> tuple[int, int] | None:
            inode = getattr(item_stat, "st_ino", 0)
            device = getattr(item_stat, "st_dev", 0)
            return (device, inode) if inode else None

        root_id = identity(root_stat)
        if root_id is not None:
            visited.add(root_id)
        pending = [root]

        while pending:
            if cancellation.is_set():
                result.is_complete = False
                result.cancelled = True
                break
            directory = pending.pop()
            result.total_directories += 1
            try:
                with os.scandir(directory) as children:
                    for child in children:
                        if cancellation.is_set():
                            result.is_complete = False
                            result.cancelled = True
                            break
                        child_path = Path(child.path)
                        result.scanned_entries += 1
                        try:
                            child_stat = child.stat(follow_symlinks=False)
                            if child.is_symlink() or _is_reparse_point(child_stat):
                                result.skipped_entries += 1
                                continue
                            if stat.S_ISDIR(child_stat.st_mode):
                                child_id = identity(child_stat)
                                if child_id is not None and child_id in visited:
                                    result.skipped_entries += 1
                                    continue
                                if child_id is not None:
                                    visited.add(child_id)
                                pending.append(child_path)
                            elif stat.S_ISREG(child_stat.st_mode):
                                result.total_files += 1
                                result.total_size += child_stat.st_size
                        except OSError as error:
                            result.skipped_entries += 1
                            result.is_complete = False
                            result.errors.append(_error_message(child_path, error))
                if result.cancelled:
                    break
            except OSError as error:
                result.skipped_entries += 1
                result.is_complete = False
                result.errors.append(_error_message(directory, error))
        return result


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _error_message(path: Path, error: OSError) -> str:
    return f"{path}: {error.strerror or str(error)}"
