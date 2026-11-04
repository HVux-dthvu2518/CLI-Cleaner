"""Async coordinator around the synchronous filesystem scanner."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Awaitable, Callable

from disk_explorer.core.scanner import FileSystemScanner
from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult

UpdateCallback = Callable[[FileEntry, ScanResult], Awaitable[None] | None]


class ScanService:
    """Run filesystem work off the UI loop with bounded worker concurrency."""

    def __init__(self, max_workers: int = 4, scanner: FileSystemScanner | None = None) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self.scanner = scanner or FileSystemScanner()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="disk-scan")
        self._active_cancellations: set[threading.Event] = set()
        self._closed = False

    async def enumerate_directory(self, path: str | Path) -> DirectoryListing:
        """Return immediate children as soon as enumeration is done."""
        self._ensure_open()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.scanner.enumerate_directory, path)

    async def calculate_directory_size(self, path: str) -> ScanResult:
        """Calculate one directory without blocking the asyncio event loop."""
        self._ensure_open()
        cancellation = threading.Event()
        self._active_cancellations.add(cancellation)
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor, self.scanner.calculate_size, path, cancellation
            )
        except asyncio.CancelledError:
            cancellation.set()
            raise
        finally:
            self._active_cancellations.discard(cancellation)

    async def calculate_child_sizes(
        self,
        entries: list[FileEntry],
        on_update: UpdateCallback | None = None,
    ) -> None:
        """Fill directory sizes incrementally, calling back after each result.

        The executor and an asyncio semaphore both enforce the configured
        concurrency. Cancellation is cooperative inside the filesystem walk.
        """
        self._ensure_open()
        directories = (
            entry
            for entry in entries
            if entry.is_directory and not entry.is_symlink and not entry.is_reparse_point
        )

        async def update_entry(entry: FileEntry) -> None:
            cancellation = threading.Event()
            self._active_cancellations.add(cancellation)
            entry.scan_status = ScanStatus.SCANNING
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    self._executor,
                    self.scanner.calculate_size,
                    entry.path,
                    cancellation,
                )
                entry.size = result.total_size if result.is_complete or result.scanned_entries else None
                entry.scan_status = ScanStatus.COMPLETE if result.is_complete else ScanStatus.PARTIAL
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

        # Only a fixed number of coordinator tasks exist, even for a listing
        # with thousands of directories; the executor has the same hard bound.
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

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("scan service is closed")

    async def __aenter__(self) -> "ScanService":
        self._ensure_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()
