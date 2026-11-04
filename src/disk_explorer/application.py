"""Textual application shell with a working, asynchronous Explorer workspace."""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from disk_explorer.analyzer import AnalyzerService
from disk_explorer.cache.cache_service import CacheService
from disk_explorer.cache.database import CacheUnavailable, Database
from disk_explorer.config import (
    AppConfig,
    cache_database_path,
    load_config,
    save_config,
    user_data_dir,
)
from disk_explorer.core.scanner import FileSystemScanner
from disk_explorer.infrastructure.logging import configure_logging
from disk_explorer.models.file_entry import FileEntry, ScanStatus
from disk_explorer.models.scan_result import ScanResult, SkippedEntry
from disk_explorer.models.skip_reason import SKIP_REASON_DETAIL, SkipReason
from disk_explorer.services.scan_service import ScanService
from disk_explorer.services.screenshot_service import save_screenshot
from disk_explorer.ui.screens.analyzer_screen import AnalyzerPanel
from disk_explorer.ui.screens.settings_screen import SettingsScreen
from disk_explorer.ui.screens.skipped_entries_screen import SkippedEntriesScreen


#: Short, unambiguous table labels for scan states.
_STATUS_LABELS = {
    ScanStatus.PENDING: "Pending",
    ScanStatus.SCANNING: "Scanning",
    ScanStatus.COMPLETE: "Complete",
    ScanStatus.PARTIAL: "Partial",
    ScanStatus.ERROR: "Error",
    ScanStatus.CACHED: "Cached",
    ScanStatus.CACHED_PARTIAL: "Cached·part",
}


def _status_label(status: ScanStatus) -> str:
    return _STATUS_LABELS.get(status, status.value.title())


def _format_size(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def _format_modified(modified_ns: int | None) -> str:
    if modified_ns is None:
        return "—"
    try:
        return datetime.fromtimestamp(modified_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


class DiskExplorerApp(App[None]):
    TITLE = "Disk Explorer & Cleanup Tool"
    SUB_TITLE = "Fast, transparent storage analysis"
    CSS_PATH = Path(__file__).resolve().parent / "ui" / "styles" / "app.tcss"
    BINDINGS = [
        Binding("1", "workspace('explorer')", "Explorer", show=False),
        Binding("2", "workspace('analyzer')", "Analyzer", show=False),
        Binding("3", "workspace('search')", "Search", show=False),
        Binding("4", "workspace('cleanup')", "Cleanup", show=False),
        Binding("enter", "open_selected", "Open"),
        Binding("backspace", "go_parent", "Up"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+l", "focus_location", "Path"),
        Binding("space", "toggle_select", "Select"),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("s", "show_skipped", "Skipped"),
        Binding("f2", "open_settings", "Settings"),
        Binding("ctrl+shift+s", "export_screenshot", "Screenshot"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config: AppConfig = load_config()
        self.logger = configure_logging()
        self.cache_service = self._build_cache_service()
        self.scan_service = ScanService(
            max_workers=self.config.max_workers,
            scanner=FileSystemScanner(
                follow_links=self.config.follow_links,
                reparse_mounts=self.config.reparse_mounts,
            ),
            cache=self.cache_service,
        )
        self.current_path = Path.home()
        self._entries: list[FileEntry] = []
        self._entries_by_path: dict[str, FileEntry] = {}
        self._size_results: dict[str, ScanResult] = {}
        self._pending_size_updates: dict[str, tuple[FileEntry, ScanResult]] = {}
        self._last_ui_refresh = 0.0
        self._selected_paths: set[str] = set()
        self._navigation_id = 0
        self._mounted = False
        self._scan_total = 0
        self._scan_done = 0
        self._scan_errors = 0
        self._scan_skipped = 0
        self._scan_partial = False
        self._listing_partial = False
        self._volume_summary = ""
        self._listing_skipped: list[SkippedEntry] = []
        self._size_skipped: list[SkippedEntry] = []
        self._listing_errors: list[str] = []
        self._refresh_requested = False
        self.logger.info("Application started", extra={"operation": "startup", "result": "ok"})

    def _build_cache_service(self) -> CacheService | None:
        """Create the cache service; a broken cache must never block browsing."""
        if not self.config.cache_enabled:
            self.logger.info("Hybrid cache disabled in settings", extra={"operation": "cache", "result": "disabled"})
            return None
        database = Database(cache_database_path(self.config.cache_path), self.logger)
        service = CacheService(database, ttl_minutes=self.config.cache_ttl_minutes, logger=self.logger)
        try:
            if not service.open():
                self.logger.warning(
                    "Cache unavailable; every scan reads the filesystem",
                    extra={"operation": "cache", "result": database.unavailable_reason or "unknown"},
                )
                return None
            service.prune()
        except (OSError, CacheUnavailable) as error:
            self.logger.warning(
                "Cache setup failed; scanning without it",
                extra={"operation": "cache", "result": str(error)},
            )
            return None
        return service

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main"):
            with TabbedContent(initial="explorer", id="workspace-tabs"):
                with TabPane("1 Explorer", id="explorer"):
                    with Vertical(id="explorer-pane"):
                        with Horizontal(id="pathbar"):
                            yield Input(str(self.current_path), placeholder="Enter a folder path", id="path-input")
                            yield Button("Go", variant="primary", id="go-button")
                            yield Button("Up", id="up-button")
                            yield Button("Refresh", id="refresh-button")
                        with Horizontal(id="explorer-layout"):
                            with Vertical(id="listing-panel"):
                                yield Static("Loading directory…", id="listing-summary")
                                yield DataTable(id="file-table", cursor_type="row", zebra_stripes=True)
                            with Vertical(id="details-panel"):
                                yield Static("Select a file or folder to inspect its details.", id="details")
                with TabPane("2 Analyzer", id="analyzer"):
                    yield AnalyzerPanel(
                        AnalyzerService(self.scan_service.scanner, self.cache_service),
                        cache=self.cache_service,
                        start_path=self.current_path,
                        id="analyzer-panel",
                    )
                with TabPane("3 Search", id="search"):
                    yield Static("[b]Search[/b]\n\nSearch by name, extension, size, or directory.", classes="workspace-copy")
                with TabPane("4 Cleanup", id="cleanup"):
                    yield Static(
                        "[b]Cleanup Center[/b]\n\nReview selected items before using the Recycle Bin or permanent deletion.",
                        classes="workspace-copy",
                    )
        yield Static("Starting Explorer…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._mounted = True
        table = self._query_base("#file-table", DataTable)
        table.add_column("Sel", key="selected", width=4)
        table.add_column("Name", key="name", width=36)
        table.add_column("Type", key="type", width=10)
        table.add_column("Size", key="size", width=14)
        table.add_column("Modified", key="modified", width=18)
        table.add_column("Status", key="scan-status", width=13)
        table.focus()
        self._apply_responsive_layout()
        self._navigate_to(self.current_path)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        if not self._mounted:
            return
        width, height = self.size
        details = self._query_base("#details-panel", Vertical)
        details.styles.display = "none" if width < 95 else "block"
        self._query_base("#listing-panel", Vertical).styles.min_width = 0 if width < 95 else 30
        self._query_base("#listing-summary", Static).styles.max_height = 1 if height < 20 else 3
        button_width = 5 if width < 80 else 7
        for button in self._query_base("#pathbar", Horizontal).query(Button):
            button.styles.min_width = button_width

    def on_unmount(self) -> None:
        self._mounted = False
        self._navigation_id += 1
        self.scan_service.close()

    def on_analyzer_panel_navigate(self, message: AnalyzerPanel.Navigate) -> None:
        """Open the Analyzer's chosen folder in the Explorer."""
        self._query_base("#workspace-tabs", TabbedContent).active = "explorer"
        self._navigate_to(message.path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go-button":
            self._navigate_to(self._query_base("#path-input", Input).value)
        elif event.button.id == "up-button":
            self.action_go_parent()
        elif event.button.id == "refresh-button":
            self.action_refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "path-input":
            self._navigate_to(event.value)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        path = str(event.row_key.value)
        entry = self._entries_by_path.get(path)
        if entry is not None:
            self._show_details(entry)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        entry = self._entries_by_path.get(str(event.row_key.value))
        if entry is not None and entry.is_directory:
            self._navigate_to(entry.path)

    def action_workspace(self, workspace: str) -> None:
        self._query_base("#workspace-tabs", TabbedContent).active = workspace

    def action_open_selected(self) -> None:
        table = self._query_base("#file-table", DataTable)
        if 0 <= table.cursor_row < len(self._entries):
            entry = self._entries[table.cursor_row]
            if entry.is_directory:
                self._navigate_to(entry.path)
            else:
                self._show_details(entry)

    def action_go_parent(self) -> None:
        parent = self.current_path.parent
        if parent != self.current_path:
            self._navigate_to(parent)

    def action_refresh(self) -> None:
        """Re-scan the current folder, ignoring any cached rows for it."""
        self._refresh_requested = True
        self._navigate_to(self.current_path)

    def action_focus_location(self) -> None:
        location = self._query_base("#path-input", Input)
        location.focus()
        location.action_select_all()

    def action_toggle_select(self) -> None:
        entry = self._highlighted_entry()
        if entry is None:
            return
        key = str(entry.path)
        table = self._query_base("#file-table", DataTable)
        if key in self._selected_paths:
            self._selected_paths.remove(key)
            table.update_cell(key, "selected", "")
        else:
            self._selected_paths.add(key)
            table.update_cell(key, "selected", "✓")
        self._set_status(f"{len(self._selected_paths)} item(s) selected · {self.current_path}")

    def action_select_all(self) -> None:
        table = self._query_base("#file-table", DataTable)
        self._selected_paths = {str(entry.path) for entry in self._entries}
        for entry in self._entries:
            table.update_cell(str(entry.path), "selected", "✓")
        self._set_status(f"Selected all {len(self._selected_paths)} visible item(s) · {self.current_path}")

    def action_open_settings(self) -> None:
        self.push_screen(SettingsScreen(self.config.screenshot_format), self._save_settings)

    def _save_settings(self, screenshot_format: str | None) -> None:
        if screenshot_format is None:
            return
        self.config.screenshot_format = screenshot_format
        try:
            config_path = save_config(self.config)
        except OSError as error:
            self._set_status(f"Could not save settings: {error}", error=True)
            return
        self._set_status(f"Settings saved · screenshots: {screenshot_format.upper()} · {config_path}")

    def action_export_screenshot(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = user_data_dir() / "screenshots" / f"disk-explorer-{timestamp}.{self.config.screenshot_format}"
        try:
            svg = self.export_screenshot()
            saved_path = save_screenshot(svg, destination, self.config.screenshot_format)
        except Exception as error:
            self.logger.exception("Screenshot export failed", extra={"operation": "screenshot", "result": "error"})
            self._set_status(f"Screenshot failed: {error}", error=True)
            return
        self._set_status(f"Screenshot saved · {saved_path}")

    def _navigate_to(self, target: str | Path) -> None:
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = self.current_path / path
        self.current_path = Path(os.path.abspath(path))
        self._navigation_id += 1
        navigation_id = self._navigation_id
        refresh = self._refresh_requested
        self._refresh_requested = False
        self._query_base("#path-input", Input).value = str(self.current_path)
        self._query_base("#file-table", DataTable).clear(columns=False)
        self._entries.clear()
        self._entries_by_path.clear()
        self._size_results.clear()
        self._pending_size_updates.clear()
        self._last_ui_refresh = 0.0
        self._selected_paths.clear()
        self._scan_total = 0
        self._scan_done = 0
        self._scan_errors = 0
        self._scan_skipped = 0
        self._scan_partial = False
        self._listing_partial = False
        self._volume_summary = ""
        self._listing_skipped: list[SkippedEntry] = []
        self._size_skipped: list[SkippedEntry] = []
        self._listing_errors: list[str] = []
        self._query_base("#details", Static).update("Select a file or folder to inspect its details.")
        self._set_status(f"Reading with {self.scan_service.engine_name.title()} engine · {self.current_path}…")
        self._query_base("#listing-summary", Static).update("Reading directory…")
        self.run_worker(
            self._load_directory(self.current_path, navigation_id, refresh),
            name="directory-scan",
            group="navigation",
            exclusive=True,
            thread=False,
        )

    async def _load_directory(self, path: Path, navigation_id: int, refresh: bool = False) -> None:
        listing = await self.scan_service.enumerate_directory(path, refresh=refresh)
        if not self.is_running or navigation_id != self._navigation_id:
            return
        self._entries = sorted(
            listing.entries,
            key=lambda item: (not item.is_directory, item.name.casefold()),
        )
        self._entries_by_path = {str(entry.path): entry for entry in self._entries}
        table = self._query_base("#file-table", DataTable)
        for index, entry in enumerate(self._entries):
            table.add_row(
                "✓" if str(entry.path) in self._selected_paths else "",
                entry.name,
                "Folder" if entry.is_directory else (entry.extension or "File"),
                "Calculating…" if entry.is_directory else _format_size(entry.size),
                _format_modified(entry.modified_ns),
                "Pending" if entry.is_directory else "Complete",
                key=str(entry.path),
            )
            if index % 100 == 99:
                await asyncio.sleep(0)
                if not self.is_running or navigation_id != self._navigation_id:
                    return
        table.focus()
        self._scan_total = sum(entry.is_directory for entry in self._entries)
        self._scan_errors = len(listing.errors)
        self._scan_skipped = listing.skipped_entries
        self._listing_skipped = list(listing.skipped_details)
        self._listing_errors = list(listing.errors)
        self._listing_partial = not listing.is_complete
        await self._update_volume_summary(path, navigation_id)
        self._update_listing_summary(False)
        for line in listing.errors[:5]:
            self.logger.warning(
                "Unreadable entry",
                extra={"operation": "enumerate", "target": line, "result": "error"},
            )
        if not listing.is_complete and not self._entries:
            self._set_status(f"Could not read directory · {len(listing.errors)} error(s) · {path}", error=True)
            return
        source = "cached listing" if listing.from_cache else f"{self.scan_service.engine_name.title()} listing"
        self._set_status(
            f"Listed {len(self._entries)} items · {source} · folder sizes running · {path}"
        )
        try:
            async def on_size_update(entry: FileEntry, result: ScanResult) -> None:
                await self._on_size_update(entry, result, navigation_id)

            await self.scan_service.calculate_child_sizes(
                self._entries, on_size_update, refresh=refresh
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._set_status(f"Folder-size scan error: {error}", error=True)
            self.logger.exception(
                "Folder-size scan failed",
                extra={"operation": "calculate-size", "target": str(path), "result": "error"},
            )
            return
        if self.is_running and navigation_id == self._navigation_id:
            self._flush_size_updates()
            self._update_listing_summary(True)
            state = "partial" if self._listing_partial or self._scan_partial or self._scan_errors else "complete"
            suffix = f" · {self._scan_errors} error(s)" if self._scan_errors else ""
            skipped = f" · {self._scan_skipped} skipped ({self._skip_summary()})" if self._scan_skipped else ""
            hint = " · press s for details" if self._scan_skipped or self._scan_errors else ""
            self._set_status(
                f"{self.scan_service.engine_name.title()} scan {state}{suffix}{skipped}{hint} · "
                f"{len(self._entries)} items · {self.current_path}",
                error=state == "partial",
            )

    async def _on_size_update(self, entry: FileEntry, result: ScanResult, navigation_id: int) -> None:
        if not self._mounted or not self.is_running or navigation_id != self._navigation_id:
            return
        key = str(entry.path)
        self._size_results[key] = result
        self._scan_done += 1
        self._scan_errors += len(result.errors)
        self._scan_skipped += result.skipped_entries
        self._scan_partial = self._scan_partial or not result.is_complete
        if result.skipped_details:
            self._size_skipped.extend(result.skipped_details)
        unreadable = [
            line
            for line in result.errors
            if "Permission denied" in line or "Access is denied" in line or "being used" in line
        ]
        for line in unreadable[:3]:
            self.logger.warning(
                "Unreadable location", extra={"operation": "calculate-size", "target": line, "result": "error"}
            )
        self._pending_size_updates[key] = (entry, result)
        now = asyncio.get_running_loop().time()
        if now - self._last_ui_refresh >= 0.1:
            self._flush_size_updates()
            self._last_ui_refresh = now

    def _flush_size_updates(self) -> None:
        if not self._mounted or not self.is_running or not self._pending_size_updates:
            return
        table = self._query_base("#file-table", DataTable)
        for key, (entry, _result) in self._pending_size_updates.items():
            if key in self._entries_by_path:
                table.update_cell(key, "size", _format_size(entry.size))
                table.update_cell(key, "scan-status", _status_label(entry.scan_status))
        self._pending_size_updates.clear()
        self._update_listing_summary(False)
        highlighted = self._highlighted_entry()
        if highlighted is not None:
            self._show_details(highlighted)

    def _highlighted_entry(self) -> FileEntry | None:
        table = self._query_base("#file-table", DataTable)
        if 0 <= table.cursor_row < len(self._entries):
            return self._entries[table.cursor_row]
        return None

    def _show_details(self, entry: FileEntry) -> None:
        kind = "Directory" if entry.is_directory else "File"
        result = self._size_results.get(str(entry.path))
        details = [
            f"[b]{entry.name}[/b]",
            f"Type: {kind}",
            f"Full path: {entry.path}",
            f"Extension: {entry.extension or '—'}",
            f"Size: {_format_size(entry.size)}",
            f"Modified: {_format_modified(entry.modified_ns)}",
            f"Scan: {entry.scan_status.value.title()}",
        ]
        if entry.scan_status is ScanStatus.CACHED and result is not None:
            details.append("Source: cached (validated by metadata and TTL)")
        if entry.scan_status is ScanStatus.CACHED_PARTIAL:
            details.append(
                "Source: cached, partial - the cached total excludes paths that could "
                "not be read when it was taken"
            )
        if entry.is_symlink or entry.is_reparse_point:
            reason = SkipReason.REPARSE_POINT if entry.is_reparse_point else SkipReason.SYMLINK
            details.append(f"Not traversed: {SKIP_REASON_DETAIL[reason]}")
        if result is not None:
            details.extend(
                [
                    f"Files: {result.total_files:,}",
                    f"Subdirectories: {max(0, result.total_directories - 1):,}",
                    f"Skipped: {result.skipped_entries:,}",
                ]
            )
            if result.skipped_details:
                details.append(f"Skipped by design: {result.skipped_summary}")
            if result.errors:
                details.append(f"Unreadable: {len(result.errors)} (press s to review)")
        self._query_base("#details", Static).update("\n\n".join(details))

    async def _update_volume_summary(self, path: Path, navigation_id: int) -> None:
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, path)
        except OSError:
            return
        if not self.is_running or navigation_id != self._navigation_id:
            return
        self._volume_summary = (
            f"Drive: {_format_size(usage.used)} used / {_format_size(usage.total)} total · "
            f"{_format_size(usage.free)} free"
        )
        self._update_listing_summary(False)

    def _update_listing_summary(self, finished: bool) -> None:
        folder_count = sum(entry.is_directory for entry in self._entries)
        file_count = len(self._entries) - folder_count
        if finished:
            is_partial = self._listing_partial or self._scan_partial or self._scan_errors
            state = "Folder sizes partial" if is_partial else "Folder sizes complete"
        else:
            state = f"Folder sizes {self._scan_done}/{self._scan_total}"
        volume = f"  ·  {self._volume_summary}" if self._volume_summary else ""
        skipped = f"  ·  {self._scan_skipped} skipped (s)" if self._scan_skipped else ""
        self._query_base("#listing-summary", Static).update(
            f"{folder_count} folders  ·  {file_count} files  ·  {state}{skipped}{volume}"
        )

    def _skip_summary(self) -> str:
        counts: dict[SkipReason, int] = {}
        for item in (*self._listing_skipped, *self._size_skipped):
            counts[item.reason] = counts.get(item.reason, 0) + 1
        ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].value))
        return ", ".join(f"{count} {reason.value}" for reason, count in ordered)

    def action_show_skipped(self) -> None:
        """Open the read-only report explaining every excluded entry."""
        combined = [*self._listing_skipped, *self._size_skipped]
        blocked = [line for result in self._size_results.values() for line in result.errors]
        blocked.extend(self._listing_errors)
        self.push_screen(SkippedEntriesScreen(self.current_path, combined, blocked))

    def _query_base(self, selector: str, expected_type: type):
        """Query the main workspace even while a modal screen is open."""
        return self.screen_stack[0].query_one(selector, expected_type)

    def _set_status(self, message: str, error: bool = False) -> None:
        if not self._mounted or not self.is_running:
            return
        widget = self._query_base("#status", Static)
        widget.update(message)
        widget.set_class(error, "error")
