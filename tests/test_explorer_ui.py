import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, Label, Static

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


@pytest.mark.asyncio
async def test_skipped_entries_are_explained_in_the_ui(tmp_path: Path) -> None:
    target = tmp_path / "Real"
    target.mkdir()
    (target / "payload.bin").write_bytes(b"x" * 16)
    link = tmp_path / "Linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/environment cannot create symlinks")
    app = DiskExplorerApp()
    app.current_path = tmp_path

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.25)
        summary = str(app.query_one("#listing-summary", Static).renderable)
        assert "1 skipped" in summary
        table = app.query_one("#file-table", DataTable)
        link_row = next(
            table.get_row_at(index) for index in range(table.row_count) if table.get_row_at(index)[1] == "Linked"
        )
        assert link_row[2] == "File"  # a link is not presented as an openable folder
        link_index = next(
            index for index in range(table.row_count) if table.get_row_at(index)[1] == "Linked"
        )
        table.move_cursor(row=link_index)
        await pilot.pause(0.05)
        details = str(app.query_one("#details", Static).renderable).lower()
        assert "not traversed" in details and "symlink" in details

        await pilot.press("s")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "SkippedEntriesScreen"
        assert "1 symlink" in str(app.screen.query_one("#skip-summary", Label).renderable)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_unreadable_paths_are_listed_in_the_report(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "secret.bin").write_bytes(b"s" * 32)
    (tmp_path / "visible.bin").write_bytes(b"v" * 4)
    try:
        os.chmod(blocked, 0o000)
    except OSError:
        pytest.skip("cannot change permissions here")
    app = DiskExplorerApp()
    app.current_path = tmp_path
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.25)
            await pilot.press("s")
            await pilot.pause()
            table = app.screen.query_one("#skip-table", DataTable)
            rows = [table.get_row_at(index) for index in range(table.row_count)]
            assert any(row[0] == "unreadable" and "blocked" in row[1] for row in rows)
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1
    finally:
        os.chmod(blocked, 0o755)
