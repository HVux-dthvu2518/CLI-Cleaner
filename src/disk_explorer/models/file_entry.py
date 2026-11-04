from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ScanStatus(str, Enum):
    PENDING = "pending"
    SCANNING = "scanning"
    COMPLETE = "complete"
    PARTIAL = "partial"
    ERROR = "error"
    CACHED = "cached"


@dataclass(slots=True)
class FileEntry:
    path: Path
    name: str
    is_directory: bool
    size: int | None
    modified_ns: int | None
    extension: str
    scan_status: ScanStatus
    is_symlink: bool = False
    is_reparse_point: bool = False
