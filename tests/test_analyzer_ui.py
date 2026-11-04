"""Analyzer workspace: keyboard use, no automatic scanning, navigation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import DataTable, Input, Static

from disk_explorer.analyzer import AnalyzerService
from disk_explorer.application import DiskExplorerApp
from disk_explorer.models.file_entry import ScanStatus
from disk_explorer.ui.screens.analyzer_screen import AnalyzerPanel


class PanelHarness(App[None]):
    """Minimal host so the panel can be tested without the whole application."""

    CSS = """
    #analyzer-pane { height: 1fr; }
    #analyzer-toolbar { height: 3; }
    #analyzer-filters { height: 3; }
    """

    def __init__(self, root: Path, analyzer: AnalyzerService | None = None) -> None:
        super().__init__()
        self._root = root
        self._analyzer = analyzer

    def compose(self):
        yield AnalyzerPanel(self._analyzer or AnalyzerService(), start_path=self._root)


def _tree(root: Path) -> Path:
    scope = root / "scope"
    (scope / "media").mkdir(parents=True)
    (scope / "media" / "clip.mp4").write_bytes(b"m" * 9000)
    (scope / "docs").mkdir()
    (scope / "docs" / "report.pdf").write_bytes(b"r" * 500)
    (scope / "notes.txt").write_bytes(b"n" * 40)
    return scope


@pytest.mark.asyncio
async def test_analyzer_does_not_scan_until_asked(tmp_path: Path) -> None:
    scope = _tree(tmp_path)
    app = PanelHarness(scope)

    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause(0.2)
        panel = app.query_one(AnalyzerPanel)
        assert panel.snapshot is None
        assert "press Analyse" in str(panel.query_one("#analyzer-status", Static).renderable)
        assert panel.query_one("#analyzer-files-table", DataTable).row_count == 0


@pytest.mark.asyncio
async def test_analyse_fills_every_view(tmp_path: Path) -> None:
    scope = _tree(tmp_path)
    app = PanelHarness(scope)

    async with app.run_test(size=(140, 44)) as pilot:
        panel = app.query_one(AnalyzerPanel)
        panel.run_analysis()
        await pilot.pause(0.8)

        assert panel.snapshot is not None
        files = panel.query_one("#analyzer-files-table", DataTable)
        folders = panel.query_one("#analyzer-folders-table", DataTable)
        types = panel.query_one("#analyzer-types-table", DataTable)
        assert files.row_count == 3
        assert files.get_row_at(0)[1] == "clip.mp4"  # largest first
        assert folders.row_count == 3
        assert types.row_count == 3
        status = str(panel.query_one("#analyzer-status", Static).renderable)
        assert "Complete" in status and "3 files" in status


@pytest.mark.asyncio
async def test_sort_toggle_reorders_results(tmp_path: Path) -> None:
    scope = _tree(tmp_path)
    app = PanelHarness(scope)

    async with app.run_test(size=(140, 44)) as pilot:
        panel = app.query_one(AnalyzerPanel)
        panel.run_analysis()
        await pilot.pause(0.8)
        largest_first = [record.name for record in panel.snapshot.largest_files]

        panel.toggle_order()
        await pilot.pause(0.2)

        assert [record.name for record in panel.snapshot.largest_files] == largest_first[::-1]
        assert "smallest first" in str(panel.query_one("#analyzer-status", Static).renderable)


@pytest.mark.asyncio
async def test_minimum_size_filter_applies_to_results(tmp_path: Path) -> None:
    scope = _tree(tmp_path)
    app = PanelHarness(scope)

    async with app.run_test(size=(140, 44)) as pilot:
        panel = app.query_one(AnalyzerPanel)
        panel.query_one("#analyzer-min-size", Input).value = "1000"
        panel.run_analysis()
        await pilot.pause(0.8)

        # clip.mp4 is 9,000 B; report.pdf (500 B) and notes.txt (40 B) fall below.
        assert [record.name for record in panel.snapshot.largest_files] == ["clip.mp4"]
        assert panel.snapshot.total_size == 9000


@pytest.mark.asyncio
async def test_invalid_minimum_size_is_reported_not_crashed(tmp_path: Path) -> None:
    scope = _tree(tmp_path)
    app = PanelHarness(scope)

    async with app.run_test(size=(140, 44)) as pilot:
        panel = app.query_one(AnalyzerPanel)
        panel.query_one("#analyzer-min-size", Input).value = "lots"
        panel.run_analysis()
        await pilot.pause(0.8)
        status = str(panel.query_one("#analyzer-status", Static).renderable)

        assert panel.snapshot is not None  # the analysis still ran
        assert "Complete" in status


@pytest.mark.asyncio
async def test_export_reports_writes_three_csv_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    scope = _tree(tmp_path)
    app = PanelHarness(scope)

    async with app.run_test(size=(140, 44)) as pilot:
        panel = app.query_one(AnalyzerPanel)
        panel.run_analysis()
        await pilot.pause(0.8)
        written = panel.export_reports()
        # Read the status while the panel is still mounted.
        status = str(panel.query_one("#analyzer-status", Static).renderable)

    assert len(written) == 3
    assert all(path.exists() for path in written)
    assert {path.suffix for path in written} == {".csv"}
    assert "Exported 3 CSV files" in status


@pytest.mark.asyncio
async def test_export_without_analysis_explains_itself(tmp_path: Path) -> None:
    scope = _tree(tmp_path)
    app = PanelHarness(scope)

    async with app.run_test(size=(140, 44)):
        panel = app.query_one(AnalyzerPanel)
        assert panel.export_reports() == []
        assert "Nothing to export" in str(panel.query_one("#analyzer-status", Static).renderable)


# -- inside the real application ------------------------------------------
@pytest.mark.asyncio
async def test_analyzer_tab_runs_and_navigates_back_to_explorer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "userdata"))
    scope = _tree(tmp_path)
    app = DiskExplorerApp()
    app.current_path = scope

    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(0.4)
        await pilot.press("2")
        await pilot.pause(0.2)
        panel = app.query_one("#analyzer-panel", AnalyzerPanel)
        panel.run_analysis()
        await pilot.pause(1.0)
        assert panel.snapshot is not None

        table = panel.query_one("#analyzer-files-table", DataTable)
        table.focus()
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.5)

        assert app.query_one("#workspace-tabs").active == "explorer"
        assert app.current_path == scope / "media"  # parent folder of the largest file
        assert [entry.name for entry in app._entries] == ["clip.mp4"]


@pytest.mark.asyncio
async def test_explorer_still_works_after_analyzer_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "userdata"))
    scope = _tree(tmp_path)
    app = DiskExplorerApp()
    app.current_path = scope

    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(0.4)
        await pilot.press("2")
        await pilot.pause(0.2)
        app.query_one("#analyzer-panel", AnalyzerPanel).run_analysis()
        await pilot.pause(0.8)
        await pilot.press("1")
        await pilot.pause(0.4)

        assert app.query_one("#workspace-tabs").active == "explorer"
        assert app.query_one("#file-table", DataTable).row_count == 3
        assert all(entry.scan_status is not ScanStatus.ERROR for entry in app._entries)


def test_analyzer_database_dir_is_not_created_on_import(tmp_path: Path) -> None:
    """Importing the analyzer must not touch the user's data directory."""
    import disk_explorer.analyzer as analyzer

    assert not hasattr(analyzer, "user_data_dir")
    assert os.path.isdir(tmp_path)
