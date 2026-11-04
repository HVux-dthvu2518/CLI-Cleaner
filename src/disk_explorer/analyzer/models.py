"""Analysis records produced from scanner output.

These models are the reusable contract for later milestones: M4 Search and M5
Cleanup should consume ``AnalysisSnapshot`` instead of walking the filesystem
themselves. Nothing here imports Textual or SQLite, and nothing here performs I/O
except the small storage probe used for the drive overview.

Rules encoded in these types:

* an unknown size is ``None``, never ``0``;
* a partial scan is described as partial, with its error list, so no consumer can
  present incomplete data as a complete report;
* folder sizes are recursive totals and must never be summed again - only files
  contribute to an aggregate total, which is what keeps totals free of
  double counting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SnapshotStatus(str, Enum):
    """How much of the requested scope the analysis actually covered."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    #: Nothing could be measured (unreadable root, missing path, no files found).
    FAILED = "failed"

    @property
    def label(self) -> str:
        return self.value.title()


class FilterKind(str, Enum):
    """Which analysis section a filter applies to."""

    FILES = "files"
    FOLDERS = "folders"
    EXTENSIONS = "extensions"


@dataclass(slots=True, frozen=True)
class FileRecord:
    """One file observed during analysis. Sizes are logical bytes (``st_size``)."""

    path: Path
    name: str
    size: int
    extension: str
    depth: int

    @property
    def parent(self) -> Path:
        return self.path.parent


@dataclass(slots=True, frozen=True)
class FolderRecord:
    """One directory and the recursive logical size measured for it."""

    path: Path
    name: str
    size: int | None
    file_count: int
    directory_count: int
    is_complete: bool
    depth: int
    errors: tuple[str, ...] = ()

    @property
    def parent(self) -> Path:
        return self.path.parent


@dataclass(slots=True, frozen=True)
class ExtensionStat:
    """Aggregate for one file extension."""

    extension: str
    file_count: int
    total_size: int
    average_size: float

    @property
    def share_of_total(self) -> float:
        """Fraction of analysed file bytes, not of disk capacity."""
        return 0.0  # replaced by the snapshot's denominator when rendered


@dataclass(slots=True, frozen=True)
class ExtensionSummary:
    """Extension statistics plus the denominator a UI needs for percentages."""

    entries: tuple[ExtensionStat, ...]
    total_size: int
    file_count: int

    def share(self, stat: ExtensionStat) -> float:
        return (stat.total_size / self.total_size) if self.total_size else 0.0


@dataclass(slots=True, frozen=True)
class StorageOverview:
    """Drive capacity next to the scanned logical total, kept deliberately distinct."""

    root: Path
    drive_total: int | None = None
    drive_used: int | None = None
    drive_free: int | None = None
    scanned_size: int = 0

    @property
    def drive_used_ratio(self) -> float:
        if not self.drive_total or self.drive_used is None:
            return 0.0
        return min(1.0, self.drive_used / self.drive_total)

    @property
    def scanned_ratio(self) -> float:
        """Analysed bytes as a share of the drive, never presented as "used"."""
        if not self.drive_total:
            return 0.0
        return min(1.0, self.scanned_size / self.drive_total) if self.drive_total else 0.0


@dataclass(slots=True, frozen=True)
class AnalysisFilters:
    """Scope filters applied while collecting records."""

    min_size: int = 0
    max_size: int | None = None
    extension: str | None = None
    directory: Path | None = None

    def includes_file(self, size: int, extension: str, path: Path) -> bool:
        if size < self.min_size:
            return False
        if self.max_size is not None and size > self.max_size:
            return False
        if self.extension is not None and _normalize_extension(extension) != _normalize_extension(
            self.extension
        ):
            return False
        if self.directory is not None and not _is_within(path, self.directory):
            return False
        return True

    @property
    def is_empty(self) -> bool:
        return (
            self.min_size <= 0
            and self.max_size is None
            and self.extension is None
            and self.directory is None
        )

    def describe(self) -> str:
        parts: list[str] = []
        if self.min_size > 0:
            parts.append(f"min {self.min_size:,} B")
        if self.max_size is not None:
            parts.append(f"max {self.max_size:,} B")
        if self.extension is not None:
            parts.append(f"extension {self.extension}")
        if self.directory is not None:
            parts.append(f"under {self.directory}")
        return ", ".join(parts)


@dataclass(slots=True)
class AnalysisSnapshot:
    """Everything the Analyzer UI needs, and the reusable input for M4/M5."""

    root: Path
    status: SnapshotStatus
    largest_files: list[FileRecord] = field(default_factory=list)
    largest_folders: list[FolderRecord] = field(default_factory=list)
    extensions: ExtensionSummary = field(
        default_factory=lambda: ExtensionSummary(entries=(), total_size=0, file_count=0)
    )
    storage: StorageOverview | None = None
    filters: AnalysisFilters = field(default_factory=AnalysisFilters)
    total_files: int = 0
    total_directories: int = 0
    total_size: int = 0
    skipped_entries: int = 0
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    from_cache: bool = False
    duration_seconds: float = 0.0
    #: Depth reached while collecting file records (bounded by configuration).
    max_depth_seen: int = 0

    @property
    def is_complete(self) -> bool:
        return self.status is SnapshotStatus.COMPLETE

    @property
    def cache_row_stored(self) -> bool:
        """Whether this snapshot's size may be cached (partial with errors is fine)."""
        return not self.cancelled and (self.status is SnapshotStatus.COMPLETE or bool(self.errors))

    def summary_line(self) -> str:
        """One-line status for the Analyzer header."""
        state = self.status.label
        coverage = f"{self.total_files:,} files · {self.total_directories:,} folders · {self.total_size:,} B"
        skipped = f" · {self.skipped_entries:,} skipped" if self.skipped_entries else ""
        problems = f" · {len(self.errors)} unreadable" if self.errors else ""
        source = " · cached" if self.from_cache else ""
        return f"{state}{source} · {coverage}{skipped}{problems}"


def _normalize_extension(value: str) -> str:
    """Canonical extension form: lowercase, leading dot, ``""`` for none."""
    text = value.strip().lower()
    if not text:
        return ""
    return text if text.startswith(".") else f".{text}"


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


normalize_extension = _normalize_extension
is_within = _is_within
