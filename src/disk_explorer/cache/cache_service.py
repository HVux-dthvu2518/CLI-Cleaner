"""Freshness policy between the filesystem scanner and the SQLite cache.

:class:`ScanService` asks this service for cached data and hands back fresh
results to store. Keeping the decision here (rather than in the UI or the
scanner) means the same rules hold for every caller:

* a directory *listing* is validated by the directory's modification time **and**
  the TTL;
* a recursive *size* is validated by TTL, root mtime, and the recorded mtimes of
  the child listings written by the same scan. A change deep inside a folder is
  invisible to a root mtime, which is exactly why TTL exists;
* nothing cancelled, partial or errored is ever stored;
* cache failures degrade to a miss, never to an exception.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from disk_explorer.cache.database import Database
from disk_explorer.cache.repository import CacheRepository, SizeRecord, mtime_ns, path_key
from disk_explorer.cache.validator import CachePolicy, Validation, ValidationStatus
from disk_explorer.models.scan_result import DirectoryListing, ScanResult


@dataclass(slots=True)
class CacheStats:
    """Counters for the status line and for honest performance claims."""

    listing_hits: int = 0
    listing_misses: int = 0
    size_hits: int = 0
    size_misses: int = 0
    partial_hits: int = 0
    skipped_writes: int = 0
    invalidations: int = 0
    last_miss_reason: str = ""
    hit_durations: list[float] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return self.listing_hits + self.size_hits

    @property
    def misses(self) -> int:
        return self.listing_misses + self.size_misses

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0

    @property
    def mean_hit_ms(self) -> float:
        if not self.hit_durations:
            return 0.0
        return (sum(self.hit_durations) / len(self.hit_durations)) * 1000.0

    def summary(self) -> str:
        if not self.hits and not self.misses:
            return "cache idle"
        partial = f" · {self.partial_hits} partial" if self.partial_hits else ""
        return (
            f"cache {self.hits} hit / {self.misses} miss "
            f"({self.hit_rate * 100:.0f}%) · {self.mean_hit_ms:.3f} ms/hit{partial}"
        )


class CacheService:
    """Decide when cached scan data may be reused, and keep it consistent."""

    def __init__(
        self,
        database: Database,
        ttl_minutes: int = 30,
        logger: logging.Logger | None = None,
        repository: CacheRepository | None = None,
    ) -> None:
        self.database = database
        self.logger = logger or logging.getLogger("disk_explorer")
        self.ttl_minutes = max(0, int(ttl_minutes))
        self.repository = repository or CacheRepository(
            database, self.logger, ttl_seconds=self.ttl_minutes * 60.0
        )
        #: Paths invalidated by a manual refresh until their fresh scan lands.
        self._invalidated: set[str] = set()
        self.stats = CacheStats()

    # -- lifecycle ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.database.available

    @property
    def policy(self) -> CachePolicy:
        return CachePolicy.from_minutes(self.ttl_minutes)

    def open(self) -> bool:
        """Open the database; ``False`` means every call will be a miss."""
        return self.database.open()

    def close(self) -> None:
        self.database.close()

    def describe_status(self) -> str:
        """One-line cache state for the UI, never raising."""
        if not self.database.available:
            return "cache off (scanning every time)"
        counts = self.repository.counts()
        return (
            f"cache {counts.get('listings', 0)} listings / {counts.get('sizes', 0)} sizes "
            f"· {self.stats.summary()}"
        )

    # -- freshness ---------------------------------------------------------
    def cached_listing(self, path: str | Path) -> DirectoryListing | None:
        """Return a cached listing only if it is still trustworthy."""
        started = time.perf_counter()
        target = path_key(path)
        if not self.database.available or target in self._invalidated:
            self._record_miss("listing", "cache unavailable" if not self.database.available else "invalidated")
            return None
        listing, validation = self.repository.load_listing(path, self.policy)
        if listing is None:
            self._record_miss("listing", validation.detail, path, validation)
            return None
        self.stats.listing_hits += 1
        self.stats.hit_durations.append(time.perf_counter() - started)
        listing.from_cache = True
        self._trace("listing-hit", path, validation)
        return listing

    def cached_listings(self, paths: list[Path]) -> dict[str, DirectoryListing]:
        """Fetch many validated listings in one sweep, keyed by :func:`path_key`.

        Rows that are missing, expired or stale are absent. A hit means the entries
        were read from disk earlier and are still trustworthy, so the caller can
        avoid touching the filesystem for that folder entirely - which is what a
        cache hit is supposed to mean.
        """
        started = time.perf_counter()
        wanted = {path_key(path): path for path in paths}
        if not self.database.available or not wanted:
            return {}
        for key in list(wanted):
            if key in self._invalidated:
                wanted.pop(key)
        if not wanted:
            return {}
        rows = self.repository.load_listings(list(wanted), self.policy)
        for key, listing in rows.items():
            listing.from_cache = True
            self.stats.listing_hits += 1
        self.stats.listing_misses += len(wanted) - len(rows)
        if rows:
            self.stats.hit_durations.append(time.perf_counter() - started)
            self.stats.last_miss_reason = ""
        return rows

    def store_listings(self, listings: list[DirectoryListing]) -> int:
        """Persist many listings at once; returns how many rows were written."""
        if not self.database.available or not listings:
            return 0
        stored = self.repository.store_listings(listings)
        for listing in listings:
            key = path_key(listing.root)
            if key in self._invalidated:
                self._invalidated.discard(key)
        return stored

    def cached_sizes(self, paths: list[Path]) -> dict[str, ScanResult]:
        """Validate many folder totals in one query, keyed by :func:`path_key`.

        The Analyzer asks about thousands of folders; one query per folder turns a
        fast cache into a bottleneck. Only rows that pass validation come back, and
        this never raises.
        """
        wanted = {path_key(path): path for path in paths}
        if not self.database.available or not wanted:
            return {}
        for key in list(wanted):
            if key in self._invalidated:
                wanted.pop(key)
        if not wanted:
            return {}
        rows = self.repository.load_size_records(list(wanted), self.policy)
        found: dict[str, ScanResult] = {}
        for key, record in rows.items():
            record.result.from_cache = True
            found[key] = record.result
            self.stats.size_hits += 1
            if not record.result.is_complete:
                self.stats.partial_hits += 1
        self.stats.size_misses += len(wanted) - len(rows)
        return found

    def cached_size(self, path: str | Path, *, cross_check_children: bool = False) -> ScanResult | None:
        """Return a cached recursive size only if it is still trustworthy.

        ``cross_check_children`` additionally requires the cached child listings
        written by the same scan to still have their recorded mtimes, which
        catches an edit in the first level of the subtree without waiting for
        the TTL.
        """
        started = time.perf_counter()
        target = path_key(path)
        if not self.database.available or target in self._invalidated:
            self._record_miss("size", "cache unavailable" if not self.database.available else "invalidated")
            return None
        record, validation = self.repository.load_size_record(path, self.policy)
        if record is None:
            self._record_miss("size", validation.detail, path, validation)
            return None
        if cross_check_children:
            changed = self._first_changed_child(record)
            if changed is not None:
                stale = Validation(ValidationStatus.STALE, f"{changed} changed since caching")
                self._record_miss("size", stale.detail, path, stale)
                return None
        self.stats.size_hits += 1
        if not record.result.is_complete:
            self.stats.partial_hits += 1
        self.stats.hit_durations.append(time.perf_counter() - started)
        record.result.from_cache = True
        self._trace("size-hit", path, validation)
        return record.result

    def _first_changed_child(self, record: SizeRecord) -> str | None:
        """Return a child path whose mtime no longer matches, if any.

        Only listings written at or after the size row are considered, so the
        check never compares against a listing from an older scan.
        """
        for child_path, stored_mtime in self.repository.listing_mtimes(
            record.created_at, record.path_key
        ):
            current = mtime_ns(child_path)
            if stored_mtime is None or current is None or current != stored_mtime:
                return child_path
        return None

    # -- writes ------------------------------------------------------------
    def store_listing(self, listing: DirectoryListing) -> bool:
        """Store a listing when it is complete and not currently invalidated."""
        if not self._storable(listing.is_complete, listing.cancelled, listing.errors):
            self.stats.skipped_writes += 1
            return False
        stored = self.repository.store_listing(listing)
        if stored:
            self._invalidated.discard(path_key(listing.root))
        return stored

    def store_size(self, result: ScanResult) -> bool:
        """Store a recursive size, including a labelled partial one.

        A partial total is worth caching: the expensive case (a large tree with a
        couple of protected folders) still costs a full walk on every visit. It is
        only ever served back with ``is_complete=False`` and its error list, so no
        caller can mistake it for a complete scan.
        """
        if not self._size_storable(result):
            self.stats.skipped_writes += 1
            return False
        stored = self.repository.store_size(result)
        if stored:
            self._invalidated.discard(path_key(result.root))
        return stored

    def _storable(self, is_complete: bool, cancelled: bool, errors: list[str]) -> bool:
        """A *listing* row may only represent a complete, error-free scan.

        Listings stay strict: a partial listing would silently hide children from
        the Explorer, and the miss costs one ``scandir``.
        """
        if not self.database.available:
            return False
        if cancelled or not is_complete or errors:
            return False
        return True

    def _size_storable(self, result: ScanResult) -> bool:
        """Sizes may be partial, but only with an explanation attached."""
        if not self.database.available:
            return False
        if result.cancelled:
            return False
        if not result.is_complete and not result.errors:
            return False
        return True

    # -- invalidation ------------------------------------------------------
    def mark_stale(self, path: str | Path, *, tree: bool = False) -> int:
        """Invalidate rows so the next request scans instead of hitting.

        Called by manual refresh. The path stays marked until the fresh result is
        stored, so a concurrent cached read cannot slip through in between.
        """
        target = Path(path)
        self._invalidated.add(path_key(target))
        removed = (
            self.repository.invalidate_tree(target) if tree else self.repository.invalidate(target)
        )
        self.stats.invalidations += 1
        self.logger.info(
            "Cache invalidated",
            extra={
                "operation": "cache-invalidate",
                "target": str(target),
                "result": f"{removed} rows",
            },
        )
        return removed

    def invalidate(self, path: str | Path) -> int:
        return self.mark_stale(path, tree=False)

    def clear(self) -> int:
        self._invalidated.clear()
        return self.repository.clear()

    def prune(self) -> int:
        """Drop expired rows; cheap enough to run on startup."""
        if not self.database.available:
            return 0
        return self.repository.prune(self.ttl_minutes * 60.0)

    # -- internals ---------------------------------------------------------
    def _record_miss(
        self,
        kind: str,
        detail: str,
        path: str | Path | None = None,
        validation: Validation | None = None,
    ) -> None:
        if kind == "listing":
            self.stats.listing_misses += 1
        else:
            self.stats.size_misses += 1
        self.stats.last_miss_reason = f"{kind}: {detail}"
        if path is not None and validation is not None:
            self._trace(f"{kind}-miss", path, validation)

    def _trace(self, operation: str, path: str | Path, validation: Validation) -> None:
        self.logger.debug(
            "Cache %s" % operation,
            extra={"operation": operation, "target": str(path), "result": validation.detail},
        )
