"""Analyzer workspace: displays analysis models, never scans by itself.

All filesystem work goes through :class:`AnalyzerService`, which already reuses the
scanner and the hybrid cache. This module only renders
:class:`~disk_explorer.analyzer.models.AnalysisSnapshot` and reacts to keys.
"""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, DataTable, Input, Static, TabbedContent, TabPane

from disk_explorer.analyzer import (
    AnalysisFilters,
    AnalysisProgress,
    AnalysisSnapshot,
    AnalyzerService,
    SnapshotStatus,
)
from disk_explorer.analyzer.report_exporter import export_all
from disk_explorer.cache.cache_service import CacheService
from disk_explorer.config import user_data_dir


def _format_size(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


class AnalyzerPanel(Container):
    """Four analysis views with filters, sorting and CSV export."""

    class Navigate(Message):
        """Ask the application to show a path in the Explorer."""

        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def __init__(
        self,
        analyzer: AnalyzerService,
        cache: CacheService | None = None,
        start_path: Path | None = None,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self.analyzer = analyzer
        self.cache = cache
        self.start_path = start_path or Path.home()
        self.snapshot: AnalysisSnapshot | None = None
        self.descending = True
        self._analysis_active = False
        self._last_export: list[Path] = []

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="analyzer-pane"):
            with Horizontal(id="analyzer-toolbar"):
                yield Input(str(self.start_path), placeholder="Folder or drive to analyse", id="analyzer-path")
                yield Button("Analyse", variant="primary", id="analyzer-run")
                yield Button("Refresh", id="analyzer-refresh")
                yield Button("Export CSV", id="analyzer-export")
                yield Button("Sort ↑↓", id="analyzer-order")
            with Horizontal(id="analyzer-filters"):
                yield Input(placeholder="Min size (bytes)", id="analyzer-min-size")
                yield Input(placeholder="Extension (.mp4)", id="analyzer-extension")
            yield Static("Choose a folder, then press Analyse. Nothing is scanned automatically.", id="analyzer-status")
            yield Static("", id="analyzer-overview")
            with TabbedContent(initial="analyzer-files", id="analyzer-tabs"):
                with TabPane("Files", id="analyzer-files"):
                    yield DataTable(id="analyzer-files-table", cursor_type="row", zebra_stripes=True)
                with TabPane("Folders", id="analyzer-folders"):
                    yield DataTable(id="analyzer-folders-table", cursor_type="row", zebra_stripes=True)
                with TabPane("File types", id="analyzer-types"):
                    yield DataTable(id="analyzer-types-table", cursor_type="row", zebra_stripes=True)
                with TabPane("Overview", id="analyzer-summary"):
                    yield Static("No analysis yet.", id="analyzer-summary-text")

    def on_mount(self) -> None:
        files = self.query_one("#analyzer-files-table", DataTable)
        files.add_column("#", key="rank", width=4)
        files.add_column("Name", key="name", width=32)
        files.add_column("Size", key="size", width=12)
        files.add_column("Type", key="ext", width=10)
        files.add_column("Folder", key="folder")

        folders = self.query_one("#analyzer-folders-table", DataTable)
        folders.add_column("#", key="rank", width=4)
        folders.add_column("Folder", key="name", width=32)
        folders.add_column("Size", key="size", width=12)
        folders.add_column("Files", key="files", width=8)
        folders.add_column("State", key="state", width=14)
        folders.add_column("Path", key="path")

        types = self.query_one("#analyzer-types-table", DataTable)
        types.add_column("Extension", key="ext", width=14)
        types.add_column("Files", key="files", width=8)
        types.add_column("Total", key="total", width=12)
        types.add_column("Average", key="average", width=12)
        types.add_column("Share", key="share", width=8)

    # -- actions -----------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "analyzer-path":
            self.run_analysis()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "analyzer-run":
            self.run_analysis()
        elif event.button.id == "analyzer-refresh":
            self.run_analysis(force_refresh=True)
        elif event.button.id == "analyzer-export":
            self.export_reports()
        elif event.button.id == "analyzer-order":
            self.toggle_order()

    def toggle_order(self) -> None:
        self.descending = not self.descending
        if self.snapshot is not None:
            from disk_explorer.analyzer.aggregators import TopFiles, TopFolders

            files = TopFiles(max(1, len(self.snapshot.largest_files)))
            for record in self.snapshot.largest_files:
                files.offer(record)
            folders = TopFolders(max(1, len(self.snapshot.largest_folders)))
            for record in self.snapshot.largest_folders:
                folders.offer(record)
            self.snapshot.largest_files = files.ranked(self.descending)
            self.snapshot.largest_folders = folders.ranked(self.descending)
            self._render_snapshot(self.snapshot)
        order = "largest first" if self.descending else "smallest first"
        self._set_status(f"Sorting: {order}")

    def run_analysis(self, force_refresh: bool = False) -> None:
        if self._analysis_active:
            return
        target = Path(self.query_one("#analyzer-path", Input).value.strip() or self.start_path).expanduser()
        filters = self._read_filters()
        self._analysis_active = True
        self._set_status(f"Analysing {target}…")
        self._run_worker(target, filters, force_refresh)

    def _read_filters(self) -> AnalysisFilters:
        minimum = self.query_one("#analyzer-min-size", Input).value.strip()
        extension = self.query_one("#analyzer-extension", Input).value.strip()
        min_size = 0
        if minimum:
            try:
                min_size = max(0, int(minimum))
            except ValueError:
                self._set_status(f"Minimum size must be a number, ignoring '{minimum}'", error=True)
        return AnalysisFilters(min_size=min_size, extension=extension or None)

    @work(exclusive=True, group="analysis", name="analyzer-scan")
    async def _run_worker(self, target: Path, filters: AnalysisFilters, force_refresh: bool) -> None:
        try:
            if force_refresh and self.cache is not None:
                self.cache.mark_stale(target)
            snapshot = await self.analyzer.analyze(
                target,
                filters=filters,
                force_refresh=force_refresh,
                on_progress=self._on_progress,
            )
        except Exception as error:  # surfaced in the status line, never swallowed
            self._analysis_active = False
            self._set_status(f"Analysis failed: {error}", error=True)
            return
        self._analysis_active = False
        self.snapshot = snapshot
        self._render_snapshot(snapshot)
        self._set_status(
            f"{snapshot.summary_line()} · {snapshot.duration_seconds:.2f}s",
            error=snapshot.status in {SnapshotStatus.FAILED, SnapshotStatus.CANCELLED},
        )

    async def _on_progress(self, progress: AnalysisProgress) -> None:
        self._set_status(f"Analysing… {progress.summary()}")

    # -- rendering ---------------------------------------------------------
    def _render_snapshot(self, snapshot: AnalysisSnapshot) -> None:
        files = self.query_one("#analyzer-files-table", DataTable)
        files.clear()
        for rank, record in enumerate(snapshot.largest_files, start=1):
            files.add_row(
                str(rank),
                record.name,
                _format_size(record.size),
                record.extension or "—",
                str(record.parent),
                key=str(record.path),
            )

        folders = self.query_one("#analyzer-folders-table", DataTable)
        folders.clear()
        for rank, record in enumerate(snapshot.largest_folders, start=1):
            folders.add_row(
                str(rank),
                record.name,
                _format_size(record.size),
                f"{record.file_count:,}",
                "Complete" if record.is_complete else "Partial",
                str(record.path),
                key=str(record.path),
            )

        types = self.query_one("#analyzer-types-table", DataTable)
        types.clear()
        for stat in snapshot.extensions.entries:
            types.add_row(
                stat.extension or "(none)",
                f"{stat.file_count:,}",
                _format_size(stat.total_size),
                _format_size(int(stat.average_size)),
                f"{snapshot.extensions.share(stat) * 100:.1f}%",
                key=stat.extension or "__none__",
            )

        self._render_overview(snapshot)
        self.query_one("#analyzer-overview", Static).update(self._overview_line(snapshot))

    def _overview_line(self, snapshot: AnalysisSnapshot) -> str:
        parts = [snapshot.status.label, f"{snapshot.total_files:,} files", f"{snapshot.total_directories:,} folders"]
        parts.append(f"{_format_size(snapshot.total_size)} analysed")
        if snapshot.skipped_entries:
            reasons = ", ".join(f"{count} {reason}" for reason, count in snapshot.skipped_by_reason.items())
            parts.append(f"{snapshot.skipped_entries:,} skipped ({reasons})")
        if snapshot.errors:
            parts.append(f"{len(snapshot.errors)} unreadable")
        if snapshot.from_cache:
            parts.append("cached")
        return "  ·  ".join(parts)

    def _render_overview(self, snapshot: AnalysisSnapshot) -> None:
        storage = snapshot.storage
        lines = [f"[b]Analysis of {snapshot.root}[/b]", ""]
        if storage is not None and storage.drive_total is not None:
            lines.append(f"Drive capacity: {_format_size(storage.drive_total)}")
            lines.append(
                f"Drive used: {_format_size(storage.drive_used)} "
                f"({storage.drive_used_ratio * 100:.1f}%) · free: {_format_size(storage.drive_free)}"
            )
        else:
            lines.append("Drive capacity: unavailable")
        lines += [
            "",
            f"Scanned logical size: {_format_size(snapshot.total_size)}",
            f"Share of drive: {storage.scanned_ratio * 100:.2f}%" if storage else "",
            f"Files: {snapshot.total_files:,} · Folders: {snapshot.total_directories:,}",
            f"Distinct extensions: {len(snapshot.extensions.entries):,}",
            f"Skipped entries: {snapshot.skipped_entries:,}",
            f"Unreadable paths: {len(snapshot.errors)}",
            f"Deepest folder analysed: {snapshot.max_depth_seen}",
            f"Duration: {snapshot.duration_seconds:.2f}s",
            "",
            "[b]Scan state[/b]: " + snapshot.status.label,
        ]
        if snapshot.status is SnapshotStatus.PARTIAL:
            lines.append("Totals exclude unreadable paths; this is not a complete report.")
        if snapshot.filters.describe():
            lines.append(f"Filters: {snapshot.filters.describe()}")
        for problem in snapshot.errors[:5]:
            lines.append(f"  ! {problem}")
        # Drive usage is allocated space; scanned size is logical bytes. Never compare them.
        lines += ["", "Drive used is allocated space; scanned size is logical bytes. They are not comparable."]
        self.query_one("#analyzer-summary-text", Static).update("\n".join(line for line in lines if line != ""))

    # -- export ------------------------------------------------------------
    def export_reports(self) -> list[Path]:
        if self.snapshot is None:
            self._set_status("Nothing to export yet; run an analysis first", error=True)
            return []
        stamp = self.snapshot.root.name or "root"
        destination = user_data_dir() / "reports" / f"{stamp}-analysis"
        try:
            self._last_export = export_all(self.snapshot, destination)
        except OSError as error:
            self._set_status(f"Export failed: {error}", error=True)
            return []
        self._set_status(f"Exported {len(self._last_export)} CSV files · {destination}")
        return self._last_export

    # -- helpers -----------------------------------------------------------
    def _set_status(self, message: str, error: bool = False) -> None:
        widget = self.query_one("#analyzer-status", Static)
        widget.update(message)
        widget.set_class(error, "error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row: show that file's folder (files) or the folder itself."""
        table_id = event.data_table.id or ""
        key = str(event.row_key.value)
        if not key or key == "__none__":
            return
        path = Path(key)
        if table_id == "analyzer-files-table":
            self.post_message(self.Navigate(path.parent))
        elif table_id == "analyzer-folders-table":
            self.post_message(self.Navigate(path))
