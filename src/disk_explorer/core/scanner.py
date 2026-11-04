"""Fast directory enumeration and iterative recursive logical-size scans."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult

# Windows marks junctions and other reparse points with this file attribute.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _error_message(path: Path, error: OSError) -> str:
    return f"{path}: {error.strerror or str(error)}"


class FileSystemScanner:
    """Filesystem-only scanner; it has no UI or persistence dependencies.

    Directory sizes are logical byte totals (`st_size`). Links/reparse points
    are never traversed. Inaccessible or changing entries make results partial.
    """

    def enumerate_directory(self, path: str | Path) -> DirectoryListing:
        """List immediate children without waiting for recursive folder sizes."""
        root = Path(path)
        result = DirectoryListing(root=root)
        try:
            with os.scandir(root) as children:
                for child in children:
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

    def calculate_size(
        self,
        path: str | Path,
        cancel_event: threading.Event | None = None,
    ) -> ScanResult:
        """Calculate a directory's logical size using an iterative traversal."""
        root = Path(path)
        result = ScanResult(root=root)
        cancellation = cancel_event or threading.Event()
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

        # Device/inode identity prevents cycles and duplicate traversal where
        # the filesystem exposes stable identifiers.
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
