from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from disk_explorer.application import DiskExplorerApp


@pytest.mark.asyncio
async def test_explorer_lists_files_and_completes_folder_sizes(tmp_path: Path) -> None:
    folder = tmp_path / "Projects"
    folder.mkdir()
    (folder / "notes.txt").write_text("notes", encoding="utf-8")
    (tmp_path / "readme.md").write_text("hello", encoding="utf-8")
    app = DiskExplorerApp()
    app.current_path = tmp_path

    async with app.run_test(size=(140, 36)) as pilot:
        await pilot.pause(0.2)
        table = app.query_one("#file-table", DataTable)
        assert table.row_count == 2
        assert [entry.name for entry in app._entries] == ["Projects", "readme.md"]
        await pilot.pause(0.2)
        folder_entry = next(entry for entry in app._entries if entry.name == "Projects")
        assert folder_entry.size == 5
        assert "1 folders" in str(app.query_one("#listing-summary", Static).renderable)


@pytest.mark.asyncio
async def test_enter_navigates_into_selected_folder(tmp_path: Path) -> None:
    folder = tmp_path / "child"
    folder.mkdir()
    (folder / "item.bin").write_bytes(b"x")
    app = DiskExplorerApp()
    app.current_path = tmp_path

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause(0.15)
        await pilot.press("enter")
        await pilot.pause(0.15)
        assert app.current_path == folder
        assert [entry.name for entry in app._entries] == ["item.bin"]
