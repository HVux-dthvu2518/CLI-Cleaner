from dataclasses import dataclass, field
from pathlib import Path

from disk_explorer.models.file_entry import FileEntry


@dataclass(slots=True)
class DirectoryListing:
    """Fast, non-recursive result for one directory."""

    root: Path
    entries: list[FileEntry] = field(default_factory=list)
    scanned_entries: int = 0
    skipped_entries: int = 0
    errors: list[str] = field(default_factory=list)
    is_complete: bool = True


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
