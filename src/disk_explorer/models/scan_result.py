from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from disk_explorer.models.file_entry import FileEntry
from disk_explorer.models.skip_reason import SkipReason


@dataclass(slots=True, frozen=True)
class SkippedEntry:
    """An entry excluded from traversal, with the reason it was excluded."""

    path: Path
    reason: SkipReason


def summarize_skips(details: Sequence["SkippedEntry"]) -> str:
    """Compact ``3 reparse point(s), 1 symlink(s)`` text for status lines."""
    counts: dict[SkipReason, int] = {}
    for item in details:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].value))
    return ", ".join(f"{count} {reason.value}(s)" for reason, count in ordered)


@dataclass(slots=True)
class DirectoryListing:
    """Fast, non-recursive result for one directory."""

    root: Path
    entries: list[FileEntry] = field(default_factory=list)
    scanned_entries: int = 0
    skipped_entries: int = 0
    errors: list[str] = field(default_factory=list)
    is_complete: bool = True
    cancelled: bool = False
    skipped_details: list[SkippedEntry] = field(default_factory=list)

    @property
    def skipped_summary(self) -> str:
        return summarize_skips(self.skipped_details)


@dataclass(slots=True)
class ScanResult:
    """Logical file-size totals for a recursive scan."""

    root: Path
    total_size: int = 0
    total_files: int = 0
    total_directories: int = 0
    scanned_entries: int = 0
    skipped_entries: int = 0
    errors: list[str] = field(default_factory=list)
    is_complete: bool = True
    cancelled: bool = False
    skipped_details: list[SkippedEntry] = field(default_factory=list)

    def skipped_count(self, reason: SkipReason) -> int:
        """Return how many entries were skipped for one reason."""
        return sum(1 for item in self.skipped_details if item.reason is reason)

    @property
    def skipped_summary(self) -> str:
        return summarize_skips(self.skipped_details)
