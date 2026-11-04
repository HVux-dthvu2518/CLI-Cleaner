from pathlib import Path

import pytest
from textual.widgets import Select, TabbedContent

from disk_explorer.application import DiskExplorerApp


@pytest.mark.asyncio
async def test_application_mounts_workspaces_and_keyboard_switches_tabs() -> None:
    app = DiskExplorerApp()
    async with app.run_test(size=(72, 18)) as pilot:
        tabs = app.query_one(TabbedContent)
        assert tabs.active == "explorer"
        await pilot.pause(0.05)
        await pilot.press("2")
        assert tabs.active == "analyzer"
        await pilot.press("4")
        assert tabs.active == "cleanup"


@pytest.mark.asyncio
async def test_settings_dialog_persists_screenshot_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    app = DiskExplorerApp()

    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.press("f2")
        screenshot_format = app.screen.query_one(Select)
        screenshot_format.value = "svg"
        await pilot.click("#save-settings")
        await pilot.pause()

        assert app.config.screenshot_format == "svg"
        assert (tmp_path / "disk-explorer-cleaner" / "settings.json").exists()
