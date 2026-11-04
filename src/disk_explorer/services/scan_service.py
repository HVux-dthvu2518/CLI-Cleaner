"""Async coordinator around the synchronous filesystem scanner.

``ScanService`` is the only place where the UI, the filesystem scanner and the
SQLite cache meet. Cache policy lives in :class:`CacheService`; this module only
decides the order of operations:

1. ask the cache (skipped for a manual refresh);
2. scan the filesystem on a bounded thread pool when the cache cannot serve;
3. store the fresh result, but only when it is complete and error-free.

With ``cache=None`` the service behaves exactly as before, so callers that do not
want persistence (tests, benchmarks, scripted scans) pay nothing for it.
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Awaitable, Callable

from disk_explorer.cache.cache_service import CacheService
from disk_explorer.core.scanner import FileSystemScanner, ScanCancellation
from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult

UpdateCallback = Callable[[FileEntry, ScanResult], Awaitable[None] | None]


def display_size(result: ScanResult) -> int | None:
    """Bytes to show for a size result, or ``None`` when the size is unknown.

    A complete scan always has a trustworthy total, including a legitimate 0.
    A partial scan only has a lower bound: show it when anything was measured,
    otherwise the size is genuinely unknown and must stay ``None`` rather than
    becoming a misleading "0 B".
    """
    if result.is_complete:
        return result.total_size
    if result.total_size or result.scanned_entries:
        return result.total_size
    return None


class ScanService:
    """Run filesystem work off the UI loop with bounded worker concurrency."""

    def __init__(
        self,
        max_workers: int = 4,
        scanner: FileSystemScanner | None = None,
        cache: CacheService | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self.scanner = scanner or FileSystemScanner()
        self.cache = cache
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="disk-scan")
        self._active_cancellations: set[ScanCancellation] = set()
        self._closed = False

    @property
    def engine_name(self) -> str:
        return self.scanner.engine_name

    @property
    def cache_enabled(self) -> bool:
        return self.cache is not None and self.cache.available

    async def enumerate_directory(self, path: str | Path, *, refresh: bool = False) -> DirectoryListing:
        """Return immediate children, from the cache when it is trustworthy.

        ``refresh=True`` invalidates the cached row first, so the result always
        comes from the filesystem.
        """
        self._ensure_open()
        if self.cache_enabled:
            assert self.cache is not None
            if refresh:
                await self._run(self.cache.mark_stale, path)
            else:
                cached = await self._run(self.cache.cached_listing, path)
                if cached is not None:
                    return cached
        cancellation = ScanCancellation()
        self._active_cancellations.add(cancellation)
        try:
            listing = await self._run(self.scanner.enumerate_directory, path, cancellation)
        except asyncio.CancelledError:
            cancellation.set()
            raise
        finally:
            self._active_cancellations.discard(cancellation)
        if self.cache_enabled and not listing.cancelled:
            assert self.cache is not None
            await self._run(self.cache.store_listing, listing)
        return listing

    async def calculate_directory_size(self, path: str | Path, *, refresh: bool = False) -> ScanResult:
        """Calculate one directory's size, from the cache when possible."""
        self._ensure_open()
        if self.cache_enabled:
            assert self.cache is not None
            if refresh:
                await self._run(self.cache.mark_stale, path)
            else:
                cached = await self._run(self.cache.cached_size, path)
                if cached is not None:
                    return cached
        cancellation = ScanCancellation()
        self._active_cancellations.add(cancellation)
        try:
            result = await self._run(self.scanner.calculate_size, path, cancellation)
        except asyncio.CancelledError:
            cancellation.set()
            raise
        finally:
            self._active_cancellations.discard(cancellation)
        if self.cache_enabled and not result.cancelled:
            assert self.cache is not None
            await self._run(self.cache.store_size, result)
        return result

    async def calculate_child_sizes(
        self,
        entries: list[FileEntry],
        on_update: UpdateCallback | None = None,
        *,
        refresh: bool = False,
    ) -> None:
        """Fill directory sizes incrementally, calling back after each result.

        A fixed number of coordinator tasks submit work to the bounded executor,
        so even a listing with thousands of folders keeps concurrency bounded.
        Cancellation is cooperative inside the filesystem walk. Cached size rows
        are reused through the same callback, so the UI does not need a second
        code path.
        """
        self._ensure_open()
        directories = (
            entry
            for entry in entries
            if entry.is_directory and not entry.is_symlink and not entry.is_reparse_point
        )

        async def update_entry(entry: FileEntry) -> None:
            if self.cache_enabled and not refresh:
                assert self.cache is not None
                cached = await self._run(self.cache.cached_size, entry.path, cross_check_children=True)
                if cached is not None:
                    entry.size = display_size(cached)
                    # A cached partial total keeps its partial label: the cache
                    # changes where the number came from, not how complete it is.
                    entry.scan_status = (
                        ScanStatus.CACHED if cached.is_complete else ScanStatus.CACHED_PARTIAL
                    )
                    if on_update is not None:
                        callback_result = on_update(entry, cached)
                        if callback_result is not None:
                            await callback_result
                    return
            cancellation = ScanCancellation()
            self._active_cancellations.add(cancellation)
            entry.scan_status = ScanStatus.SCANNING
            try:
                result = await self._run(self.scanner.calculate_size, entry.path, cancellation)
                entry.size = display_size(result)
                entry.scan_status = ScanStatus.COMPLETE if result.is_complete else ScanStatus.PARTIAL
                if self.cache_enabled and not result.cancelled:
                    assert self.cache is not None
                    await self._run(self.cache.store_size, result)
                if on_update is not None:
                    callback_result = on_update(entry, result)
                    if callback_result is not None:
                        await callback_result
            except asyncio.CancelledError:
                cancellation.set()
                entry.scan_status = ScanStatus.PENDING
                raise
            finally:
                self._active_cancellations.discard(cancellation)

        async def worker() -> None:
            while True:
                try:
                    entry = next(directories)
                except StopIteration:
                    return
                await update_entry(entry)

        tasks = [asyncio.create_task(worker()) for _ in range(self.max_workers)]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def invalidate_cache(self, path: str | Path, *, tree: bool = False) -> int:
        """Drop cached rows for a path; used by manual refresh and by deletion."""
        if not self.cache_enabled:
            return 0
        assert self.cache is not None
        return self.cache.mark_stale(path, tree=tree)

    def cancel_all(self) -> None:
        """Request cancellation of active recursive scans."""
        for event in tuple(self._active_cancellations):
            event.set()

    def close(self) -> None:
        """Stop accepting work and request running scans to finish promptly."""
        if self._closed:
            return
        self._closed = True
        self.cancel_all()
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self.cache is not None:
            self.cache.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("scan service is closed")

    async def _run(self, function: Callable[..., object], *args: object, **kwargs: object) -> object:
        """Run a blocking cache/scanner call on the bounded executor.

        Cache reads are sub-millisecond, but they still touch the filesystem and
        SQLite, so they must not run on the event loop.
        """
        loop = asyncio.get_running_loop()
        if kwargs:
            return await loop.run_in_executor(
                self._executor, functools.partial(function, *args, **kwargs)
            )
        return await loop.run_in_executor(self._executor, function, *args)

    async def __aenter__(self) -> "ScanService":
        self._ensure_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()
