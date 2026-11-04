"""Fast enumeration and recursive-size scanning with a Rust acceleration path.

The extension releases Python's GIL during filesystem traversal. The Python
implementation remains a compatible fallback for source runs without a built
extension and for callers supplying their own threading.Event.

Both engines report *why* an entry was not traversed. Symlinks and Windows
reparse points are never followed, but they are recorded with a
:class:`SkipReason` so the UI can explain the exclusion instead of hiding it.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import Any

from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult, SkippedEntry
from disk_explorer.models.skip_reason import SkipReason

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

    def __init__(self, follow_links: bool = False, reparse_mounts: bool = False) -> None:
        """Create a scanner.

        ``follow_links`` opts in to traversing symbolic links. ``reparse_mounts``
        opts in to traversing reparse points that are *not* symbolic links (for
        example the junctions Windows uses for redirected user folders). Both
        default to ``False``, which is the safe "never follow links" policy.
        """
        self.follow_links = follow_links
        self.reparse_mounts = reparse_mounts

    @property
    def engine_name(self) -> str:
        return "rust" if _rust_scanner is not None else "python"

    @property
    def traversal_options(self) -> tuple[bool, bool]:
        """Options shared with the Rust engine, in (follow_links, reparse_mounts) order."""
        return (self.follow_links, self.reparse_mounts)

    # -- decisions ---------------------------------------------------------
    def _skip_reason(
        self,
        is_symlink: bool,
        is_reparse: bool,
        *,
        was_followed: bool,
    ) -> SkipReason | None:
        """Return why a non-directory child is excluded, or ``None`` if counted.

        A traversed symlink/reparse point yields ``None`` so it is counted like
        an ordinary entry. Otherwise the more specific of the two attributes
        wins: reparse points keep their Windows-specific wording.
        """
        if was_followed:
            return None
        if is_reparse:
            return SkipReason.REPARSE_POINT
        if is_symlink:
            return SkipReason.SYMLINK
        return None

    def _may_traverse(self, is_symlink: bool, is_reparse: bool) -> bool:
        """Whether the configured policy allows descending into this child."""
        if is_symlink:
            return self.follow_links
        if is_reparse:
            return self.reparse_mounts or self.follow_links
        return True

    # -- public API --------------------------------------------------------
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
                (
                    entries,
                    scanned,
                    errors,
                    complete,
                    cancelled,
                    skip_records,
                ) = _rust_scanner.enumerate_directory(
                    str(root), native_token, self.follow_links, self.reparse_mounts
                )
                return self._listing_from_rust(
                    root, entries, scanned, errors, complete, cancelled, skip_records
                )
        return self._enumerate_python(root, cancellation)

    def _listing_from_rust(
        self,
        root: Path,
        entries: list[tuple[Any, ...]],
        scanned: int,
        errors: list[str],
        complete: bool,
        cancelled: bool,
        skip_records: list[tuple[str, str]],
    ) -> DirectoryListing:
        skipped_details = _skip_records_to_entries(skip_records)
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
            skipped_entries=len(skipped_details),
            errors=errors,
            is_complete=complete,
            cancelled=cancelled,
            skipped_details=skipped_details,
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
                    errors,
                    is_complete,
                    cancelled,
                    skip_records,
                ) = _rust_scanner.calculate_size(
                    str(root), native_token, self.follow_links, self.reparse_mounts
                )
                skipped_details = _skip_records_to_entries(skip_records)
                return ScanResult(
                    root=root,
                    total_size=total_size,
                    total_files=total_files,
                    total_directories=total_directories,
                    scanned_entries=scanned_entries,
                    skipped_entries=len(skipped_details),
                    errors=errors,
                    is_complete=is_complete,
                    cancelled=cancelled,
                    skipped_details=skipped_details,
                )
        return self._calculate_size_python(root, cancellation)

    # -- Python fallback ---------------------------------------------------
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
        except OSError as error:
            result.is_complete = False
            result.skipped_entries = 1
            result.errors.append(_error_message(root, error))
            return result
        root_is_link = stat.S_ISLNK(root_stat.st_mode) or _is_reparse_point(root_stat)
        if not stat.S_ISDIR(root_stat.st_mode) and not root_is_link:
            result.is_complete = False
            result.skipped_entries = 1
            result.errors.append(f"{root}: not a directory")
            return result
        root_symlink = stat.S_ISLNK(root_stat.st_mode)
        root_reparse = _is_reparse_point(root_stat)
        root_reason = self._skip_reason(
            root_symlink, root_reparse, was_followed=self._may_traverse(root_symlink, root_reparse)
        )
        if root_reason is not None:
            result.is_complete = False
            result.skipped_details.append(SkippedEntry(path=root, reason=root_reason))
            result.skipped_entries = 1
            result.errors.append(_root_not_traversed(root, root_reason))
            return result
        try:
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
                        followed = self._may_traverse(is_symlink, is_reparse)
                        is_directory_stat = stat.S_ISDIR(item_stat.st_mode)
                        if followed and (is_symlink or is_reparse):
                            is_directory_stat = child.is_dir(follow_symlinks=True)
                        # A link is a folder in the table only when we actually
                        # traverse it; otherwise it stays a non-openable link row.
                        is_directory = is_directory_stat and (followed or not (is_symlink or is_reparse))
                        result.entries.append(
                            FileEntry(
                                path=child_path,
                                name=child.name,
                                is_directory=is_directory,
                                size=None if is_directory else item_stat.st_size,
                                modified_ns=getattr(item_stat, "st_mtime_ns", None),
                                extension="" if is_directory else child_path.suffix,
                                scan_status=ScanStatus.PENDING if is_directory else ScanStatus.COMPLETE,
                                is_symlink=is_symlink,
                                is_reparse_point=is_reparse,
                            )
                        )
                        reason = self._skip_reason(is_symlink, is_reparse, was_followed=followed)
                        if reason is not None:
                            result.skipped_details.append(SkippedEntry(path=child_path, reason=reason))
                    except OSError as error:
                        result.skipped_entries += 1
                        result.is_complete = False
                        result.errors.append(_error_message(child_path, error))
        except OSError as error:
            result.is_complete = False
            result.skipped_entries += 1
            result.errors.append(_error_message(root, error))
        result.skipped_entries += len(result.skipped_details)
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

        root_is_link = stat.S_ISLNK(root_stat.st_mode) or _is_reparse_point(root_stat)
        if not stat.S_ISDIR(root_stat.st_mode) and not root_is_link:
            result.is_complete = False
            result.skipped_entries = 1
            result.errors.append(f"{root}: not a directory")
            return result

        root_symlink = stat.S_ISLNK(root_stat.st_mode)
        root_reparse = _is_reparse_point(root_stat)
        root_reason = self._skip_reason(
            root_symlink, root_reparse, was_followed=self._may_traverse(root_symlink, root_reparse)
        )
        if root_reason is not None:
            result.is_complete = False
            result.skipped_entries = 1
            result.skipped_details.append(SkippedEntry(path=root, reason=root_reason))
            result.errors.append(_root_not_traversed(root, root_reason))
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
                            is_symlink = child.is_symlink()
                            is_reparse = _is_reparse_point(child_stat)
                            followed = self._may_traverse(is_symlink, is_reparse)
                            reason = self._skip_reason(is_symlink, is_reparse, was_followed=followed)
                            if reason is not None:
                                result.skipped_details.append(SkippedEntry(path=child_path, reason=reason))
                                continue
                            stats_for_size = child_stat
                            if followed and (is_symlink or is_reparse):
                                # The caller opted in, so measure the link target.
                                stats_for_size = child.stat(follow_symlinks=True)
                            if stat.S_ISDIR(stats_for_size.st_mode):
                                child_id = identity(stats_for_size)
                                if child_id is not None and child_id in visited:
                                    result.skipped_details.append(
                                        SkippedEntry(path=child_path, reason=SkipReason.DUPLICATE_DIRECTORY)
                                    )
                                    continue
                                if child_id is not None:
                                    visited.add(child_id)
                                pending.append(child_path)
                            elif stat.S_ISREG(stats_for_size.st_mode):
                                result.total_files += 1
                                result.total_size += stats_for_size.st_size
                            else:
                                result.skipped_details.append(
                                    SkippedEntry(path=child_path, reason=SkipReason.NON_REGULAR_FILE)
                                )
                        except OSError as error:
                            result.is_complete = False
                            result.errors.append(_error_message(child_path, error))
                if result.cancelled:
                    break
            except OSError as error:
                result.is_complete = False
                result.errors.append(_error_message(directory, error))
        result.skipped_entries = len(result.skipped_details) + len(result.errors)
        return result


def _skip_records_to_entries(records: list[tuple[str, str]]) -> list[SkippedEntry]:
    """Convert ``(path, reason)`` pairs from the Rust engine into model objects."""
    converted: list[SkippedEntry] = []
    for path, reason in records:
        try:
            skip_reason = SkipReason(reason)
        except ValueError:  # A newer engine reported a reason this build does not know.
            continue
        converted.append(SkippedEntry(path=Path(path), reason=skip_reason))
    return converted


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _error_message(path: Path, error: OSError) -> str:
    return f"{path}: {error.strerror or str(error)}"


def _root_not_traversed(path: Path, reason: SkipReason) -> str:
    return f"{path}: not traversed ({reason.value}); enable link traversal to scan it"
