"""CSV export for analysis results.

Three independent reports are written from one snapshot. Every export states the
snapshot's status in the file, so a CSV of a partial scan cannot be mistaken for a
complete one after it leaves the application.
"""

from __future__ import annotations

import csv
from pathlib import Path

from disk_explorer.analyzer.models import AnalysisSnapshot, SnapshotStatus

_BOM = "\ufeff"


def export_files(snapshot: AnalysisSnapshot, destination: str | Path) -> Path:
    """Largest files: rank, name, extension, size, folder, full path."""
    path = Path(destination)
    with _open(path) as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "name", "extension", "size_bytes", "folder", "path"])
        for rank, record in enumerate(snapshot.largest_files, start=1):
            writer.writerow(
                [rank, record.name, record.extension or "", record.size, str(record.parent), str(record.path)]
            )
        _status_row(writer, snapshot)
    return path


def export_folders(snapshot: AnalysisSnapshot, destination: str | Path) -> Path:
    """Largest folders: rank, name, recursive size, counts, completeness, path."""
    path = Path(destination)
    with _open(path) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["rank", "name", "size_bytes", "files", "subfolders", "complete", "depth", "path"]
        )
        for rank, record in enumerate(snapshot.largest_folders, start=1):
            writer.writerow(
                [
                    rank,
                    record.name,
                    "" if record.size is None else record.size,  # unknown stays blank, not 0
                    record.file_count,
                    max(0, record.directory_count - 1),
                    "yes" if record.is_complete else "no",
                    record.depth,
                    str(record.path),
                ]
            )
        _status_row(writer, snapshot)
    return path


def export_extensions(snapshot: AnalysisSnapshot, destination: str | Path) -> Path:
    """Extension statistics with the share of analysed bytes."""
    path = Path(destination)
    summary = snapshot.extensions
    with _open(path) as handle:
        writer = csv.writer(handle)
        writer.writerow(["extension", "files", "total_size_bytes", "average_bytes", "share_of_total"])
        for stat in summary.entries:
            writer.writerow(
                [
                    stat.extension or "(none)",
                    stat.file_count,
                    stat.total_size,
                    f"{stat.average_size:.1f}",
                    f"{summary.share(stat) * 100:.2f}",
                ]
            )
        writer.writerow([])
        writer.writerow(["total", summary.file_count, summary.total_size, "", "100.00"])
        _status_row(writer, snapshot)
    return path


def export_all(snapshot: AnalysisSnapshot, directory: str | Path, stem: str = "disk-analysis") -> list[Path]:
    """Write all three reports into ``directory`` and return their paths."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    return [
        export_files(snapshot, folder / f"{stem}-files.csv"),
        export_folders(snapshot, folder / f"{stem}-folders.csv"),
        export_extensions(snapshot, folder / f"{stem}-extensions.csv"),
    ]


class _open:  # noqa: N801 - context manager used as a callable
    """Open a CSV file as UTF-8 with a BOM, so Excel renders non-ASCII paths."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" is required by the csv module to avoid blank lines on Windows.
        self._handle = open(self.path, "w", encoding="utf-8-sig", newline="")
        return self._handle

    def __exit__(self, *_exc) -> None:
        if self._handle is not None:
            self._handle.close()


def _status_row(writer, snapshot: AnalysisSnapshot) -> None:
    """Trailing metadata row: a CSV must say how complete it is."""
    writer.writerow([])
    writer.writerow(["scan_status", snapshot.status.value])
    writer.writerow(["root", str(snapshot.root)])
    writer.writerow(["total_files", snapshot.total_files])
    writer.writerow(["total_directories", snapshot.total_directories])
    writer.writerow(["total_size_bytes", snapshot.total_size])
    writer.writerow(["skipped_entries", snapshot.skipped_entries])
    writer.writerow(["unreadable_paths", len(snapshot.errors)])
    if snapshot.status is SnapshotStatus.PARTIAL:
        writer.writerow(["warning", "partial scan: totals exclude unreadable paths"])
    elif snapshot.status is SnapshotStatus.CANCELLED:
        writer.writerow(["warning", "cancelled scan: totals are incomplete"])
    if snapshot.filters.describe():
        writer.writerow(["filters", snapshot.filters.describe()])
