"""Analysis orchestration built on the existing scanner and hybrid cache.

This module never scans by itself: it walks with :class:`FileSystemScanner`, asks
the :class:`ScanService` cache for folder totals it already knows, and turns the
result into the models in :mod:`disk_explorer.analyzer.models`.

Progress is reported through the same ``on_update``-style callback the Explorer
uses, so the Textual event loop only ever receives small messages.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from disk_explorer.analyzer.aggregators import Counters, ExtensionAccumulator, TopFiles, TopFolders
from disk_explorer.analyzer.models import (
    AnalysisFilters,
    AnalysisSnapshot,
    FileRecord,
    FolderRecord,
    SnapshotStatus,
    StorageOverview,
)
from disk_explorer.cache.cache_service import CacheService
from disk_explorer.core.scanner import FileSystemScanner, ScanCancellation
from disk_explorer.models.scan_result import ScanResult

ProgressCallback = Callable[["AnalysisProgress"], Awaitable[None] | None]


@dataclass(slots=True, frozen=True)
class AnalysisProgress:
    """Incremental progress handed to the UI while a scan runs."""

    files_seen: int
    directories_seen: int
    bytes_seen: int
    skipped: int
    errors: int
    current_depth: int
    elapsed: float

    def summary(self) -> str:
        return (
            f"{self.files_seen:,} files · {self.directories_seen:,} folders · "
            f"{self.bytes_seen:,} B · {self.elapsed:.1f}s"
        )


@dataclass(slots=True)
class AnalyzerLimits:
    """Bounded work limits, so a huge tree degrades predictably instead of thrashing."""

    #: Directories analysed in detail. Beyond this, only totals keep accumulating.
    max_directories: int = 20000
    #: Recursion depth cap; Windows paths above this are usually junctions or runaway.
    max_depth: int = 40
    #: Emit progress at most this often (seconds).
    progress_interval: float = 0.15
    #: How often to consult the cache for a folder's recursive size.
    cache_probe_directories: int = 2000


class AnalyzerService:
    """Produce an :class:`AnalysisSnapshot` for one root directory.

    The walk is breadth-first on purpose: folder totals come from the scanner (or
    the cache), and depth is bounded, so a pathological tree cannot consume
    unbounded memory.
    """

    def __init__(
        self,
        scanner: FileSystemScanner | None = None,
        cache: CacheService | None = None,
        limits: AnalyzerLimits | None = None,
    ) -> None:
        self.scanner = scanner or FileSystemScanner()
        self.cache = cache
        self.limits = limits or AnalyzerLimits()

    async def analyze(
        self,
        root: str | Path,
        *,
        top_files: int = 100,
        top_folders: int = 50,
        filters: AnalysisFilters | None = None,
        force_refresh: bool = False,
        on_progress: ProgressCallback | None = None,
        cancellation: ScanCancellation | None = None,
    ) -> AnalysisSnapshot:
        """Walk ``root`` and return a ranked, filtered snapshot.

        ``force_refresh`` skips the cache for folder totals but still uses it for
        plain listings, so a refresh stays fast while never serving a stale number.
        """
        target = Path(root).expanduser()
        filters = filters or AnalysisFilters()
        token = cancellation or ScanCancellation()
        started = time.perf_counter()

        files = TopFiles(top_files)
        folders = TopFolders(top_folders)
        extensions = ExtensionAccumulator()
        counters = Counters()

        if not target.is_dir():
            return AnalysisSnapshot(
                root=target,
                status=SnapshotStatus.FAILED,
                errors=[f"{target}: not a readable directory"],
                filters=filters,
                duration_seconds=time.perf_counter() - started,
            )

        depth_limited = False
        hit_limit = False
        last_progress = 0.0
        analysed_directories = 0
        root_result: ScanResult | None = None
        cache_probes = 0

        # (directory, depth, inherited_errors): the parent's errors are propagated
        # down, so a folder inside an unreadable branch is still labelled partial.
        pending: list[tuple[Path, int, tuple[str, ...]]] = [(target, 0, ())]
        while pending:
            if token.is_set():
                counters.cancelled = True
                break
            directory, depth, inherited = pending.pop(0)
            analysed_directories += 1
            if analysed_directories > self.limits.max_directories:
                hit_limit = True
                break
            counters.add_directory(depth)

            listing = self.scanner.enumerate_directory(directory, token)
            if listing.cancelled:
                counters.cancelled = True
                break
            counters.add_errors(listing.errors)
            counters.add_skips(
                _count_reasons(item.reason.value for item in listing.skipped_details),
                listing.skipped_entries,
            )
            if depth >= self.limits.max_depth and any(entry.is_directory for entry in listing.entries):
                depth_limited = True

            for entry in listing.entries:
                if entry.is_directory:
                    child_errors = inherited + tuple(listing.errors)
                    pending.append((entry.path, depth + 1, child_errors))
                    continue
                size = entry.size
                if size is None:  # Unknown is never treated as zero.
                    continue
                if not filters.includes_file(size, entry.extension, entry.path):
                    continue
                record = FileRecord(
                    path=entry.path,
                    name=entry.name,
                    size=size,
                    extension=entry.extension,
                    depth=depth,
                )
                files.offer(record)
                extensions.add(record.extension, record.size)
                counters.add_file(record)

            # Folder total for this directory: reuse a valid cached size, else scan.
            known_result: ScanResult | None = None
            if self.cache is not None and cache_probes < self.limits.cache_probe_directories:
                cache_probes += 1
                known_result = self.cache.cached_size(directory)
                if force_refresh:
                    known_result = None
            if known_result is None:
                known_result = self.scanner.calculate_size(directory, token)
                if self.cache is not None and not known_result.cancelled:
                    self.cache.store_size(known_result)
            if directory == target:
                root_result = known_result
            inherited_for_folder = inherited + tuple(known_result.errors)
            folders.offer(
                FolderRecord(
                    path=directory,
                    name=directory.name or str(directory),
                    size=_display_size(known_result),
                    file_count=known_result.total_files,
                    directory_count=known_result.total_directories,
                    is_complete=known_result.is_complete,
                    depth=depth,
                    errors=inherited_for_folder,
                )
            )
            if known_result.cancelled:
                counters.cancelled = True
                break

            now = time.perf_counter()
            if on_progress is not None and now - last_progress >= self.limits.progress_interval:
                last_progress = now
                await _emit(
                    on_progress,
                    AnalysisProgress(
                        files_seen=counters.files,
                        directories_seen=counters.directories,
                        bytes_seen=counters.size,
                        skipped=counters.skipped,
                        errors=len(counters.errors),
                        current_depth=depth,
                        elapsed=now - started,
                    ),
                )

        status = _status(counters, root_result, files.offered)
        snapshot = AnalysisSnapshot(
            root=target,
            status=status,
            largest_files=files.ranked(),
            largest_folders=folders.ranked(),
            extensions=_extension_summary(extensions),
            storage=self._storage_overview(target, counters.size),
            filters=filters,
            total_files=counters.files,
            total_directories=counters.directories,
            total_size=counters.size,
            skipped_entries=counters.skipped,
            skipped_by_reason=dict(counters.skipped_by_reason),
            errors=list(dict.fromkeys(counters.errors)),
            cancelled=counters.cancelled,
            duration_seconds=time.perf_counter() - started,
            max_depth_seen=counters.max_depth,
        )
        if hit_limit:
            snapshot.errors.append(
                f"analysis stopped after {self.limits.max_directories:,} folders; "
                "results cover the first part of the tree only"
            )
            snapshot.status = SnapshotStatus.PARTIAL
        if depth_limited:
            snapshot.errors.append(
                f"folders deeper than {self.limits.max_depth} levels were not analysed separately"
            )
            snapshot.status = (
                SnapshotStatus.PARTIAL if snapshot.status is SnapshotStatus.COMPLETE else snapshot.status
            )
        return snapshot

    def analyze_sync(self, root: str | Path, **kwargs: object) -> AnalysisSnapshot:
        """Synchronous convenience wrapper for scripts and benchmarks."""
        return asyncio.run(self.analyze(root, **kwargs))  # type: ignore[arg-type]

    def _storage_overview(self, root: Path, scanned: int) -> StorageOverview:
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            return StorageOverview(root=root, scanned_size=scanned)
        return StorageOverview(
            root=root,
            drive_total=usage.total,
            drive_used=usage.used,
            drive_free=usage.free,
            scanned_size=scanned,
        )


def _status(counters: Counters, root_result: ScanResult | None, files_seen: int) -> SnapshotStatus:
    """Decide how honestly the scope was covered."""
    if counters.cancelled:
        return SnapshotStatus.CANCELLED
    if root_result is None or (root_result.total_size == 0 and root_result.total_files == 0 and counters.errors):
        return SnapshotStatus.FAILED if not files_seen and not counters.directories else SnapshotStatus.PARTIAL
    if counters.errors:
        return SnapshotStatus.PARTIAL
    if root_result is not None and not root_result.is_complete:
        return SnapshotStatus.PARTIAL
    return SnapshotStatus.COMPLETE


def _display_size(result: ScanResult) -> int | None:
    """Mirror ``scan_service.display_size`` without importing the service layer."""
    if result.is_complete:
        return result.total_size
    if result.total_size or result.scanned_entries:
        return result.total_size
    return None


def _extension_summary(accumulator: ExtensionAccumulator):
    from disk_explorer.analyzer.models import ExtensionSummary

    return ExtensionSummary(
        entries=tuple(accumulator.ranked()),
        total_size=accumulator.total_size,
        file_count=accumulator.file_count,
    )


def _count_reasons(reasons) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return counts


async def _emit(callback: ProgressCallback, progress: AnalysisProgress) -> None:
    outcome = callback(progress)
    if outcome is not None:
        await outcome
