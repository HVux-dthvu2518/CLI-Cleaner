import pytest
from textual.widgets import TabbedContent

from disk_explorer.application import DiskExplorerApp


@pytest.mark.asyncio
async def test_application_mounts_workspaces_and_keyboard_switches_tabs() -> None:
    app = DiskExplorerApp()
    async with app.run_test(size=(72, 18)) as pilot:
        tabs = app.query_one(TabbedContent)
        assert tabs.active == "explorer"
        await pilot.press("2")
        assert tabs.active == "analyzer"
        await pilot.press("4")
        assert tabs.active == "cleanup"
