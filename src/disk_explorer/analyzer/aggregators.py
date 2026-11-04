"""Bounded aggregators: top-N rankings and streaming bookkeeping.

A full directory walk can touch hundreds of thousands of files, so nothing here
keeps ``n`` items in memory when the caller asked for a fixed shortlist.

* ``TopFiles`` / ``TopFolders`` keep a heap of exactly ``limit`` items.
* ``Counters`` accumulates totals that must never be derived from folder sizes.
* ``ExtensionAccumulator`` groups by extension with a single pass.

Ordering is deliberately explicit: **size descending, then path ascending**. Equal
sizes always break the same way, so reports are stable between runs and tests can
assert exact order. Paths are compared case-insensitively to match how Windows
displays them, with the original path as the final tiebreaker.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from disk_explorer.analyzer.models import ExtensionStat, FileRecord, FolderRecord, normalize_extension


class _Rankable(Protocol):
    path: Path


def _order_key(path: Path) -> str:
    """Stable, case-insensitive tiebreaker used only after the size comparison."""
    return str(path).casefold()


class _TopN:
    """Heap of at most ``limit`` items, ordered by size desc then path asc."""

    __slots__ = ("_limit", "_heap", "_sign", "offered", "kept")

    def __init__(self, limit: int, descending: bool = True) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        # The heap root is the item that gets evicted first: the *smallest* size
        # when keeping the largest N, and the largest size when keeping the
        # smallest N. Hence +1 for descending, -1 for ascending.
        self._sign = 1 if descending else -1
        self._heap: list[tuple[int, str, object]] = []
        self.offered = 0
        self.kept = 0

    def _priority(self, size: int, path: Path) -> tuple[int, str]:
        # Smallest priority is evicted first. Descending: smallest size loses;
        # ascending: largest size loses. Ties evict the alphabetically later path.
        return (self._sign * size, _invert_order(_order_key(path)))

    def offers(self, item: object, size: int, path: Path) -> None:
        self.offered += 1
        priority = self._priority(size, path)
        if len(self._heap) < self._limit:
            heapq.heappush(self._heap, (priority[0], priority[1], item))
            return
        if priority > self._heap[0][:2]:
            heapq.heapreplace(self._heap, (priority[0], priority[1], item))

    def results(self, size_of, descending: bool = True) -> list:
        items = sorted(
            (entry[2] for entry in self._heap),
            key=lambda item: (
                -size_of(item) if descending else size_of(item),
                _order_key(item.path),
            ),
        )
        self.kept = len(items)
        return items


def _invert_order(key: str) -> tuple[int, ...]:
    """Make ``max``-style heap ordering evict the alphabetically latest path first."""
    return tuple(-ord(character) for character in key)


class TopFiles(_TopN):
    """Keep the ``limit`` largest (or smallest) files seen."""

    def offer(self, record: FileRecord) -> None:
        self.offers(record, record.size, record.path)

    def ranked(self, descending: bool = True) -> list[FileRecord]:
        return self.results(lambda record: record.size, descending)  # type: ignore[return-value]


class TopFolders(_TopN):
    """Keep the ``limit`` largest (or smallest) folders seen."""

    def offer(self, record: FolderRecord) -> None:
        # A folder whose size is unknown competes as the smallest possible value,
        # so it is never ranked above a measured folder.
        self.offers(record, record.size if record.size is not None else -1, record.path)

    def ranked(self, descending: bool = True) -> list[FolderRecord]:
        return self.results(
            lambda record: record.size if record.size is not None else -1, descending
        )  # type: ignore[return-value]


@dataclass(slots=True)
class Counters:
    """Totals that must be computed from files only, never from folder totals."""

    files: int = 0
    directories: int = 0
    size: int = 0
    skipped: int = 0
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    max_depth: int = 0

    def add_file(self, record: FileRecord) -> None:
        self.files += 1
        self.size += record.size
        self.max_depth = max(self.max_depth, record.depth)

    def add_directory(self, depth: int) -> None:
        self.directories += 1
        self.max_depth = max(self.max_depth, depth)

    def add_skips(self, counts: dict[str, int], total: int) -> None:
        self.skipped += total
        for reason, count in counts.items():
            self.skipped_by_reason[reason] = self.skipped_by_reason.get(reason, 0) + count

    def add_errors(self, errors: Iterable[str]) -> None:
        self.errors.extend(errors)


class ExtensionAccumulator:
    """Group files by extension in one streaming pass."""

    __slots__ = ("_counts", "_sizes", "total_size", "file_count")

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._sizes: dict[str, int] = {}
        self.total_size = 0
        self.file_count = 0

    def add(self, extension: str, size: int) -> None:
        key = normalize_extension(extension)
        self._counts[key] = self._counts.get(key, 0) + 1
        self._sizes[key] = self._sizes.get(key, 0) + size
        self.total_size += size
        self.file_count += 1

    def ranked(self, limit: int | None = None) -> list[ExtensionStat]:
        """Aggregates sorted by size desc, then name asc (stable for equal sizes)."""
        stats = [
            ExtensionStat(
                extension=extension,
                file_count=count,
                total_size=self._sizes[extension],
                average_size=(self._sizes[extension] / count) if count else 0.0,
            )
            for extension, count in self._counts.items()
        ]
        stats.sort(key=lambda stat: (-stat.total_size, stat.extension))
        return stats[:limit] if limit else stats

    @property
    def distinct_extensions(self) -> int:
        return len(self._counts)
