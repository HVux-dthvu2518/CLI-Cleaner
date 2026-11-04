"""Disk Analyzer: reusable analysis models and services on top of the scanner.

Layering:

``models``          FileRecord, FolderRecord, ExtensionStat, AnalysisSnapshot
``aggregators``     bounded top-N heaps and streaming extension counters
``analyzer_service`` walks with FileSystemScanner, reuses the hybrid cache
``report_exporter`` CSV output for files, folders and extension statistics

The package never imports Textual. M4 Search and M5 Cleanup should consume
``AnalysisSnapshot`` rather than walking the filesystem again.
"""

from disk_explorer.analyzer.analyzer_service import (
    AnalysisProgress,
    AnalyzerLimits,
    AnalyzerService,
)
from disk_explorer.analyzer.models import (
    AnalysisFilters,
    AnalysisSnapshot,
    ExtensionStat,
    ExtensionSummary,
    FileRecord,
    FolderRecord,
    SnapshotStatus,
    StorageOverview,
)

__all__ = [
    "AnalysisFilters",
    "AnalysisProgress",
    "AnalysisSnapshot",
    "AnalyzerLimits",
    "AnalyzerService",
    "ExtensionStat",
    "ExtensionSummary",
    "FileRecord",
    "FolderRecord",
    "SnapshotStatus",
    "StorageOverview",
]
