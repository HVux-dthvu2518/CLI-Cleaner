"""Read/write/delete access to cached listings and recursive sizes.

The repository is deliberately dumb about policy: it stores and loads rows and
translates them into the application's models. Whether a row is *acceptable* is
decided by :mod:`disk_explorer.cache.validator`, so the two concerns can be
tested (and changed) independently.

Rows are keyed by ``os.path.normcase(os.path.abspath(path))`` so ``C:\\Temp``,
``c:/temp`` and ``C:\\Temp\\`` share one entry on Windows, matching filesystem
semantics. Every method swallows cache failures and reports them as "not stored"
or "miss"; the cache must never be the reason a scan fails.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from disk_explorer.cache.database import CacheUnavailable, Database
from disk_explorer.cache.validator import CachePolicy, Validation, ValidationStatus
from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult, SkippedEntry
from disk_explorer.models.skip_reason import SkipReason

_CACHE_ERRORS = (CacheUnavailable, sqlite3.Error)


@dataclass(slots=True, frozen=True)
class SizeRecord:
    """A recursive size row joined with its validation window."""

    result: ScanResult
    path_key: str
    created_at: float
    mtime_ns: int | None
    ttl_seconds: float
    is_complete: bool = True


def path_key(path: str | Path) -> str:
    """Normalized cache key for a path."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def mtime_ns(path: str | Path) -> int | None:
    """Modification time in ns, or ``None`` when it cannot be read.

    ``None`` means "unknown", which the validator treats as uncacheable rather
    than as "unchanged".
    """
    try:
        return os.stat(os.fspath(path), follow_symlinks=False).st_mtime_ns
    except OSError:
        return None


def _skip_payload(details: list[SkippedEntry]) -> str:
    return json.dumps([[str(item.path), item.reason.value] for item in details])


def _skip_details(raw: str) -> list[SkippedEntry]:
    try:
        pairs = json.loads(raw or "[]")
    except ValueError:
        return []
    details: list[SkippedEntry] = []
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        try:
            reason = SkipReason(pair[1])
        except ValueError:
            continue  # Unknown reason from a newer build: ignore rather than fail.
        details.append(SkippedEntry(path=Path(pair[0]), reason=reason))
    return details


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a path prefix is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_prefix(key: str) -> str:
    """``LIKE`` pattern matching everything beneath ``key`` (escaped)."""
    return _escape_like(key.rstrip("\\/") + os.sep) + "%"


class CacheRepository:
    """Persist and retrieve listings and sizes; never raises on cache failure."""

    def __init__(
        self,
        database: Database,
        logger: logging.Logger | None = None,
        ttl_seconds: float = 1800.0,
    ) -> None:
        self.database = database
        self.logger = logger or logging.getLogger("disk_explorer")
        #: TTL recorded with every row written from now on.
        self.ttl_seconds = max(0.0, float(ttl_seconds))

    # -- listings ----------------------------------------------------------
    def store_listing(self, listing: DirectoryListing) -> bool:
        """Store a directory listing.

        Only complete results are stored. Cancelled, partial or errored listings
        are skipped: a cache hit must never resurrect a partial scan as if it were
        a successful one.
        """
        if not listing.is_complete or listing.cancelled or listing.errors:
            return False
        root_mtime = mtime_ns(listing.root)
        if root_mtime is None:
            return False
        payload = json.dumps(
            [
                [
                    entry.name,
                    entry.is_directory,
                    entry.size,
                    entry.modified_ns,
                    entry.extension,
                    entry.is_symlink,
                    entry.is_reparse_point,
                ]
                for entry in listing.entries
            ]
        )
        try:
            with self.database.transaction(write=True) as connection:
                connection.execute(
                    """
                    INSERT INTO listings
                        (path_key, path, created_at, ttl_seconds, mtime_ns,
                         entry_count, skipped_count, payload, skipped)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path_key) DO UPDATE SET
                        path=excluded.path,
                        created_at=excluded.created_at,
                        ttl_seconds=excluded.ttl_seconds,
                        mtime_ns=excluded.mtime_ns,
                        entry_count=excluded.entry_count,
                        skipped_count=excluded.skipped_count,
                        payload=excluded.payload,
                        skipped=excluded.skipped
                    """,
                    (
                        path_key(listing.root),
                        str(listing.root),
                        time.time(),
                        self.ttl_seconds,
                        root_mtime,
                        len(listing.entries),
                        listing.skipped_entries,
                        payload,
                        _skip_payload(listing.skipped_details),
                    ),
                )
            return True
        except _CACHE_ERRORS as error:
            self._warn("cache-write-listing", error)
            return False

    def load_listing(
        self, path: str | Path, policy: CachePolicy, now: float | None = None
    ) -> tuple[DirectoryListing | None, Validation]:
        """Return a validated cached listing, or ``(None, reason)``."""
        root = Path(path)
        try:
            row = self.database.fetch_one(
                """
                SELECT created_at, ttl_seconds, mtime_ns, payload, skipped
                FROM listings WHERE path_key = ?
                """,
                (path_key(root),),
            )
        except _CACHE_ERRORS as error:
            self._warn("cache-read-listing", error)
            return None, Validation(ValidationStatus.UNCACHEABLE, str(error))
        if row is None:
            return None, Validation(ValidationStatus.UNCACHEABLE, "no cached listing")
        created_at, row_ttl, stored_mtime, payload, skipped = row
        validation = policy.validate(
            created_at=created_at,
            row_ttl_seconds=row_ttl,
            stored_mtime_ns=stored_mtime,
            current_mtime_ns=mtime_ns(root),
            kind="listing",
            now=time.time() if now is None else now,
        )
        if not validation.is_valid:
            return None, validation
        try:
            entries = [
                FileEntry(
                    path=root / name,
                    name=name,
                    is_directory=is_directory,
                    size=size,
                    modified_ns=modified_ns,
                    extension=extension,
                    scan_status=ScanStatus.CACHED if is_directory else ScanStatus.COMPLETE,
                    is_symlink=is_symlink,
                    is_reparse_point=is_reparse,
                )
                for (
                    name,
                    is_directory,
                    size,
                    modified_ns,
                    extension,
                    is_symlink,
                    is_reparse,
                ) in json.loads(payload)
            ]
        except (ValueError, TypeError) as error:
            self._warn("cache-decode-listing", error)
            return None, Validation(ValidationStatus.UNCACHEABLE, "cached listing is unreadable")
        details = _skip_details(skipped)
        return (
            DirectoryListing(
                root=root,
                entries=entries,
                scanned_entries=len(entries),
                skipped_entries=len(details),
                is_complete=True,
                cancelled=False,
                skipped_details=details,
            ),
            validation,
        )

    def listing_mtimes(self, since: float, prefix: str) -> list[tuple[str, int | None]]:
        """Cached child listings written at or after ``since`` under ``prefix``."""
        try:
            return [
                (row[0], row[1])
                for row in self.database.fetch_all(
                    "SELECT path, mtime_ns FROM listings WHERE created_at >= ? AND path_key LIKE ? ESCAPE '\\'",
                    (since, _like_prefix(prefix)),
                )
            ]
        except _CACHE_ERRORS:
            return []

    # -- recursive sizes ---------------------------------------------------
    def store_size(self, result: ScanResult) -> bool:
        """Store a recursive size result, including a *labelled* partial one.

        A partial total is stored with ``is_complete=0`` and its error list, so a
        reader can never mistake it for a complete scan. What is never stored is a
        result the walker abandoned (cancelled), or a "partial" result that carries
        no explanation - that would be an unaccountable number.
        """
        if result.cancelled:
            return False
        if not result.is_complete and not result.errors:
            return False
        root_mtime = mtime_ns(result.root)
        if root_mtime is None:
            return False
        try:
            with self.database.transaction(write=True) as connection:
                connection.execute(
                    """
                    INSERT INTO sizes
                        (path_key, path, created_at, ttl_seconds, mtime_ns, total_size,
                         total_files, total_directories, scanned_entries, skipped_count, skipped,
                         is_complete, errors)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path_key) DO UPDATE SET
                        path=excluded.path,
                        created_at=excluded.created_at,
                        ttl_seconds=excluded.ttl_seconds,
                        mtime_ns=excluded.mtime_ns,
                        total_size=excluded.total_size,
                        total_files=excluded.total_files,
                        total_directories=excluded.total_directories,
                        scanned_entries=excluded.scanned_entries,
                        skipped_count=excluded.skipped_count,
                        skipped=excluded.skipped,
                        is_complete=excluded.is_complete,
                        errors=excluded.errors
                    """,
                    (
                        path_key(result.root),
                        str(result.root),
                        time.time(),
                        self.ttl_seconds,
                        root_mtime,
                        result.total_size,
                        result.total_files,
                        result.total_directories,
                        result.scanned_entries,
                        result.skipped_entries,
                        _skip_payload(result.skipped_details),
                        1 if result.is_complete else 0,
                        json.dumps(result.errors),
                    ),
                )
            return True
        except _CACHE_ERRORS as error:
            self._warn("cache-write-size", error)
            return False

    def load_size(
        self, path: str | Path, policy: CachePolicy, now: float | None = None
    ) -> tuple[ScanResult | None, Validation]:
        """Return a validated cached size total, or ``(None, reason)``."""
        record, validation = self.load_size_record(path, policy, now)
        return (record.result if record else None), validation

    def load_size_record(
        self, path: str | Path, policy: CachePolicy, now: float | None = None
    ) -> tuple[SizeRecord | None, Validation]:
        """Like :meth:`load_size`, but keeps the row's own window for revalidation."""
        root = Path(path)
        try:
            row = self.database.fetch_one(
                """
                SELECT created_at, ttl_seconds, mtime_ns, total_size, total_files,
                       total_directories, scanned_entries, skipped_count, skipped,
                       is_complete, errors
                FROM sizes WHERE path_key = ?
                """,
                (path_key(root),),
            )
        except _CACHE_ERRORS as error:
            self._warn("cache-read-size", error)
            return None, Validation(ValidationStatus.UNCACHEABLE, str(error))
        if row is None:
            return None, Validation(ValidationStatus.UNCACHEABLE, "no cached size")
        (
            created_at,
            row_ttl,
            stored_mtime,
            total_size,
            total_files,
            total_directories,
            scanned_entries,
            skipped_count,
            skipped,
            is_complete,
            errors,
        ) = row
        validation = policy.validate(
            created_at=created_at,
            row_ttl_seconds=row_ttl,
            stored_mtime_ns=stored_mtime,
            current_mtime_ns=mtime_ns(root),
            kind="size",
            now=time.time() if now is None else now,
        )
        if not validation.is_valid:
            return None, validation
        try:
            stored_errors = [str(line) for line in json.loads(errors or "[]")]
        except (ValueError, TypeError):
            stored_errors = ["cached error list was unreadable"]
        complete = bool(is_complete) and not stored_errors
        result = ScanResult(
            root=root,
            total_size=int(total_size),
            total_files=int(total_files),
            total_directories=int(total_directories),
            scanned_entries=int(scanned_entries),
            skipped_entries=int(skipped_count),
            errors=stored_errors,
            is_complete=complete,
            cancelled=False,
            skipped_details=_skip_details(skipped),
        )
        return (
            SizeRecord(
                result=result,
                path_key=path_key(root),
                created_at=created_at,
                mtime_ns=stored_mtime,
                ttl_seconds=float(row_ttl or 0.0),
                is_complete=complete,
            ),
            validation,
        )

    # -- invalidation ------------------------------------------------------
    def invalidate(
        self,
        path: str | Path,
        *,
        include_sizes: bool = True,
        include_listings: bool = True,
    ) -> int:
        """Drop cached data for one path. Returns the number of removed rows."""
        removed = 0
        try:
            with self.database.transaction(write=True) as connection:
                if include_listings:
                    removed += connection.execute(
                        "DELETE FROM listings WHERE path_key = ?", (path_key(path),)
                    ).rowcount
                if include_sizes:
                    removed += connection.execute(
                        "DELETE FROM sizes WHERE path_key = ?", (path_key(path),)
                    ).rowcount
        except _CACHE_ERRORS as error:
            self._warn("cache-invalidate", error)
            return 0
        return max(0, removed)

    def invalidate_tree(self, path: str | Path) -> int:
        """Drop cached rows for a path and everything beneath it.

        Used by manual refresh, where trusting any descendant would defeat the
        point of asking for fresh data.
        """
        key = path_key(path)
        pattern = _like_prefix(key)
        removed = 0
        try:
            with self.database.transaction(write=True) as connection:
                for table in ("listings", "sizes"):
                    removed += connection.execute(
                        f"DELETE FROM {table} WHERE path_key = ? OR path_key LIKE ? ESCAPE '\\'",
                        (key, pattern),
                    ).rowcount
        except _CACHE_ERRORS as error:
            self._warn("cache-invalidate-tree", error)
            return 0
        return max(0, removed)

    def clear(self) -> int:
        """Empty the cache. Rows are disposable, so this never raises."""
        removed = 0
        try:
            with self.database.transaction(write=True) as connection:
                for table in ("listings", "sizes"):
                    removed += connection.execute(f"DELETE FROM {table}").rowcount
        except _CACHE_ERRORS as error:
            self._warn("cache-clear", error)
            return 0
        return max(0, removed)

    def prune(self, ttl_seconds: float, now: float | None = None) -> int:
        """Delete rows whose own recorded TTL has passed (opportunistic cleanup)."""
        removed = 0
        reference = time.time() if now is None else now
        try:
            with self.database.transaction(write=True) as connection:
                for table in ("listings", "sizes"):
                    removed += connection.execute(
                        f"DELETE FROM {table} WHERE ttl_seconds > 0 AND created_at < ?",
                        (reference,),
                    ).rowcount
                    removed += connection.execute(
                        f"DELETE FROM {table} WHERE ttl_seconds <= 0 AND ? > 0 AND created_at < ?",
                        (max(0.0, ttl_seconds), reference - max(0.0, ttl_seconds)),
                    ).rowcount
        except _CACHE_ERRORS as error:
            self._warn("cache-prune", error)
            return 0
        return max(0, removed)

    def counts(self) -> dict[str, int]:
        """Row counts per table, for diagnostics and the status line."""
        try:
            return self.database.table_counts()
        except _CACHE_ERRORS:
            return {"listings": 0, "sizes": 0}

    # -- internals ---------------------------------------------------------
    def _warn(self, operation: str, error: BaseException) -> None:
        self.logger.warning(
            "Cache operation failed; using a fresh scan",
            extra={"operation": operation, "target": "cache", "result": str(error)},
        )
