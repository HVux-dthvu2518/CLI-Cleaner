"""Analysis orchestration built on the existing scanner and hybrid cache.

This module never scans by itself: it walks with :class:`FileSystemScanner`, reads
folder *listings* from the :class:`CacheService` when they are still valid, and
turns the result into the models in :mod:`disk_explorer.analyzer.models`.

**Two phases, linear work.** An earlier revision asked the scanner for every
folder's recursive size while walking. Each of those calls re-walks the subtree, so
the cost was O(folders x tree): on a real 16,652-folder tree that was ~15 s.

1. *Walk* - enumerate each directory exactly once, in sweeps. Each sweep asks the
   cache for its listings in a single query and enumerates only the folders the
   cache could not answer, so a warm run does not touch the disk for unchanged
   folders.
2. *Totals* - every folder's recursive size is the bottom-up sum of the direct file
   bytes already collected, plus its children's totals. No subtree is ever re-walked.

Progress is reported through a small callback, so the Textual event loop only ever
receives short messages.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from disk_explorer.analyzer.aggregators import Counters, ExtensionAccumulator, TopFiles, TopFolders
from disk_explorer.analyzer.models import (
    AnalysisFilters,
    AnalysisSnapshot,
    ExtensionSummary,
    FileRecord,
    FolderRecord,
    SnapshotStatus,
    StorageOverview,
)
from disk_explorer.cache.cache_service import CacheService
from disk_explorer.cache.repository import path_key
from disk_explorer.core.scanner import FileSystemScanner, ScanCancellation
from disk_explorer.models.scan_result import DirectoryListing, ScanResult

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
    #: ``walking`` while enumerating, ``sizing`` while summing subtree totals.
    phase: str = "walking"

    def summary(self) -> str:
        phase = "sizing" if self.phase == "sizing" else "scanning"
        return (
            f"{phase} · {self.files_seen:,} files · {self.directories_seen:,} folders · "
            f"{_format_bytes(self.bytes_seen)} · {self.elapsed:.1f}s"
        )


@dataclass(slots=True)
class AnalyzerLimits:
    """Bounded work limits, so a huge tree degrades predictably instead of thrashing."""

    #: Directories descended into. Beyond this, the report is marked partial.
    max_directories: int = 50_000
    #: Recursion depth cap; Windows paths beyond this are usually junctions.
    max_depth: int = 64
    #: Emit progress at most this often (seconds).
    progress_interval: float = 0.15
    #: Directories handled in one batched cache read/write sweep.
    listing_sweep: int = 256


@dataclass(slots=True)
class _FolderNode:
    """What one directory contributes to the tree, collected during the walk."""

    path: Path
    depth: int
    #: Bytes and files directly inside this folder (not its subfolders).
    direct_size: int = 0
    direct_files: int = 0
    child_directories: list[Path] = field(default_factory=list)
    errors: tuple[str, ...] = ()
    has_listing_errors: bool = False
    #: True when the folder's own entries came from the filesystem or cache.
    enumerated: bool = True


@dataclass(slots=True, frozen=True)
class _SubtreeTotal:
    """A folder's recursive totals, or a cached stand-in for them."""

    size: int
    files: int
    directories: int
    complete: bool
    #: True when the numbers came straight from a cache row instead of the walk.
    measured: bool = False

    def as_result(self, root: Path) -> ScanResult:
        return ScanResult(
            root=root,
            total_size=self.size,
            total_files=self.files,
            total_directories=self.directories,
            scanned_entries=self.files,
            skipped_entries=0,
            errors=[] if self.complete else ["cached total was labelled incomplete"],
            is_complete=self.complete,
            cancelled=False,
        )


class AnalyzerService:
    """Produce an :class:`AnalysisSnapshot` for one root directory.

    Breadth-first with a depth cap, so a pathological tree cannot consume unbounded
    memory. This class is deliberately free of Textual imports: M4 Search and M5
    Cleanup consume :class:`AnalysisSnapshot` the same way the UI does.
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

        ``force_refresh`` ignores both cached listings and cached totals, so the
        numbers always come from a real walk even if that costs a rescan.
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

        nodes: dict[Path, _FolderNode] = {}
        served_from_cache: set[Path] = set()
        target_children: list[Path] = []
        order: list[Path] = []
        pending: list[Path] = [target]
        visited: set[Path] = {target}
        last_progress = 0.0
        depth_limited = False
        use_cache = self.cache is not None and not force_refresh
        sweep_size = max(1, self.limits.listing_sweep)

        # ---- phase 1: one enumeration per directory -------------------------
        while pending:
            if token.is_set():
                counters.cancelled = True
                break
            sweep = pending[:sweep_size]
            del pending[:sweep_size]
            if len(order) + len(sweep) > self.limits.max_directories:
                sweep = sweep[: max(0, self.limits.max_directories - len(order))]
                counters.limit_hit = True
            if not sweep:
                break

            cached: dict[str, DirectoryListing] = {}
            if use_cache:
                cached = self.cache.cached_listings(sweep)  # type: ignore[union-attr]

            fresh: list[DirectoryListing] = []
            for directory in sweep:
                if token.is_set():
                    counters.cancelled = True
                    break
                counters.add_directory(0)
                listing = cached.get(path_key(directory))
                if listing is None:
                    listing = self.scanner.enumerate_directory(directory, token)
                    if listing.cancelled:
                        counters.cancelled = True
                        break
                    if listing.is_complete and not listing.errors:
                        fresh.append(listing)
                else:
                    counters.cache_hits += 1
                    served_from_cache.add(directory)

                depth = len(directory.relative_to(target).parts) if directory != target else 0
                if depth >= self.limits.max_depth and any(
                    entry.is_directory for entry in listing.entries
                ):
                    depth_limited = True

                node = _FolderNode(
                    path=directory,
                    depth=depth,
                    errors=tuple(listing.errors),
                    has_listing_errors=bool(listing.errors),
                )
                counters.add_errors(listing.errors)
                counters.add_skips(
                    _count_reasons(item.reason.value for item in listing.skipped_details),
                    listing.skipped_entries,
                )
                if directory == target:
                    target_children = [
                        entry.path for entry in listing.entries if entry.is_directory
                    ]

                for entry in listing.entries:
                    if entry.is_directory:
                        node.child_directories.append(entry.path)
                        if entry.path not in visited and depth + 1 <= self.limits.max_depth:
                            visited.add(entry.path)
                            pending.append(entry.path)
                        continue
                    size = entry.size
                    if size is None:  # Unknown is never treated as zero.
                        continue
                    node.direct_size += size
                    node.direct_files += 1
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

                nodes[directory] = node
                order.append(directory)

            if fresh and use_cache:
                self.cache.store_listings(fresh)  # type: ignore[union-attr]

            now = time.perf_counter()
            if on_progress is not None and (
                now - last_progress >= self.limits.progress_interval or not pending
            ):
                last_progress = now
                await _emit(
                    on_progress,
                    AnalysisProgress(
                        files_seen=counters.files,
                        directories_seen=len(order),
                        bytes_seen=counters.size,
                        skipped=counters.skipped,
                        errors=len(counters.errors),
                        current_depth=0,
                        elapsed=now - started,
                        phase="walking",
                    ),
                )

        # ---- phase 2: recursive totals from the walk itself -----------------
        totals = self._subtree_totals(nodes, order)

        # A folder whose children were never enumerated (depth cap or limit) has no
        # subtree data. A valid cache row is the only honest source left for it.
        unresolved = [path for path in order if path not in totals]
        if unresolved and self.cache is not None and not force_refresh:
            cached_sizes = self.cache.cached_sizes(unresolved)
            for path in unresolved:
                result = cached_sizes.get(path_key(path))
                if result is None:
                    continue
                size = _display_size(result)
                totals[path] = _SubtreeTotal(
                    size=size if size is not None else 0,
                    files=result.total_files,
                    directories=result.total_directories,
                    complete=result.is_complete,
                    measured=True,
                )

        # Feed the cache so the Explorer benefits from this walk. Folders whose
        # listing came from the cache already have a stored total, so rewriting
        # them would cost more than the walk it is meant to save.
        if self.cache is not None and not force_refresh and totals:
            self._store_totals(totals, unchanged=served_from_cache)

        # ---- build the snapshot --------------------------------------------
        for directory in order:
            folders.offer(_folder_record(nodes[directory], totals.get(directory)))

        root_total = totals.get(target)
        children = sorted(
            (
                _folder_record(nodes[child], totals.get(child))
                for child in target_children
                if child in nodes
            ),
            key=lambda record: (record.size is None, -(record.size or 0), record.name.casefold()),
        )
        snapshot = AnalysisSnapshot(
            root=target,
            status=_status(counters, root_total, bool(order)),
            largest_files=files.ranked(),
            largest_folders=folders.ranked(),
            children=children,
            extensions=_extension_summary(extensions),
            storage=self._storage_overview(target, counters.size),
            filters=filters,
            total_files=counters.files,
            total_directories=len(order),
            total_size=counters.size,
            skipped_entries=counters.skipped,
            skipped_by_reason=dict(counters.skipped_by_reason),
            errors=list(dict.fromkeys(counters.errors)),
            cancelled=counters.cancelled,
            from_cache=bool(order) and len(served_from_cache) == len(order),
            cached_directories=counters.cache_hits,
            duration_seconds=time.perf_counter() - started,
            max_depth_seen=counters.max_depth,
        )
        if counters.limit_hit:
            snapshot.errors.append(
                f"analysis stopped after {self.limits.max_directories:,} folders; "
                "raise AnalyzerLimits.max_directories to cover the whole tree"
            )
            snapshot.status = SnapshotStatus.PARTIAL
        if depth_limited:
            snapshot.errors.append(
                f"folders deeper than {self.limits.max_depth} levels were not analysed separately"
            )
            if snapshot.status is SnapshotStatus.COMPLETE:
                snapshot.status = SnapshotStatus.PARTIAL
        return snapshot

    # -- sizing ------------------------------------------------------------
    def _subtree_totals(
        self, nodes: dict[Path, _FolderNode], order: list[Path]
    ) -> dict[Path, _SubtreeTotal]:
        """Bottom-up subtree sums: size, files and folder counts per directory."""
        totals: dict[Path, _SubtreeTotal] = {}
        # Deepest first, so children are always complete before their parent.
        for directory in sorted(order, key=lambda path: nodes[path].depth, reverse=True):
            node = nodes[directory]
            size = node.direct_size
            files = node.direct_files
            directories = 1
            complete = not node.has_listing_errors
            for child in node.child_directories:
                child_total = totals.get(child)
                if child_total is None:
                    # Not enumerated (depth cap or limit): this folder cannot claim
                    # to know its own subtree either.
                    complete = False
                    continue
                size += child_total.size
                files += child_total.files
                directories += child_total.directories
                complete = complete and child_total.complete
            totals[directory] = _SubtreeTotal(
                size=size,
                files=files,
                directories=directories,
                complete=complete,
            )
        return totals

    def _store_totals(
        self, totals: dict[Path, _SubtreeTotal], unchanged: set[Path] | None = None
    ) -> None:
        """Persist complete totals so later runs (and the Explorer) reuse them.

        Only complete totals are written: a subtree that hit a permission error or a
        limit must not be cached as if it were fully measured. ``unchanged`` lists
        folders whose listing was served from the cache; their stored total is still
        inside its window, so writing it again buys nothing.
        """
        if self.cache is None:
            return
        skip = unchanged or set()
        for directory, total in totals.items():
            if not total.complete or (directory in skip and not total.measured):
                continue
            self.cache.store_size(total.as_result(directory))

    # -- helpers -----------------------------------------------------------
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


def _folder_record(node: _FolderNode, total: _SubtreeTotal | None) -> FolderRecord:
    """One folder row; unknown stays ``None`` rather than becoming zero."""
    return FolderRecord(
        path=node.path,
        name=node.path.name or str(node.path),
        size=total.size if total is not None else None,
        file_count=total.files if total is not None else node.direct_files,
        directory_count=total.directories if total is not None else 1,
        is_complete=total.complete if total is not None else not node.has_listing_errors,
        depth=node.depth,
        errors=node.errors,
    )


def _status(
    counters: Counters,
    root_total: _SubtreeTotal | None,
    directories: bool,
) -> SnapshotStatus:
    """Decide how honestly the scope was covered."""
    if counters.cancelled:
        return SnapshotStatus.CANCELLED
    if not directories:
        return SnapshotStatus.FAILED
    if counters.errors:
        return SnapshotStatus.PARTIAL
    if root_total is None or not root_total.complete:
        return SnapshotStatus.PARTIAL
    return SnapshotStatus.COMPLETE


def _display_size(result: ScanResult) -> int | None:
    """Mirror ``scan_service.display_size`` without importing the service layer."""
    if result.is_complete:
        return result.total_size
    if result.total_size or result.scanned_entries:
        return result.total_size
    return None


def _extension_summary(accumulator: ExtensionAccumulator) -> ExtensionSummary:
    return ExtensionSummary(
        entries=tuple(accumulator.ranked()),
        total_size=accumulator.total_size,
        file_count=accumulator.file_count,
    )


def _count_reasons(reasons: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


async def _emit(callback: ProgressCallback, progress: AnalysisProgress) -> None:
    outcome = callback(progress)
    if outcome is not None:
        await outcome
