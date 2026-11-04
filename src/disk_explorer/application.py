"""Textual application shell for the four planned workspaces."""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from disk_explorer.config import load_config
from disk_explorer.infrastructure.logging import configure_logging


class DiskExplorerApp(App[None]):
    TITLE = "Disk Explorer & Cleanup Tool"
    SUB_TITLE = "Fast, transparent storage analysis"
    CSS_PATH = Path(__file__).resolve().parent / "ui" / "styles" / "app.tcss"
    BINDINGS = [
        Binding("1", "workspace('explorer')", "Explorer", show=False),
        Binding("2", "workspace('analyzer')", "Analyzer", show=False),
        Binding("3", "workspace('search')", "Search", show=False),
        Binding("4", "workspace('cleanup')", "Cleanup", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.logger = configure_logging()
        self.logger.info("Application started", extra={"operation": "startup", "result": "ok"})

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main"):
            with TabbedContent(initial="explorer", id="workspace-tabs"):
                with TabPane("1 Explorer", id="explorer"):
                    yield Static(
                        "[b]Explorer[/b]\n\n"
                        "Filesystem browsing is the next UI milestone. The scan engine is ready.\n"
                        "Choose a directory to begin; recursive sizes will be calculated in the background.",
                        classes="workspace-copy",
                    )
                with TabPane("2 Analyzer", id="analyzer"):
                    yield Static(
                        "[b]Disk Analyzer[/b]\n\n"
                        "Drive overview, largest folders, files, and extension totals will appear here.",
                        classes="workspace-copy",
                    )
                with TabPane("3 Search", id="search"):
                    yield Static(
                        "[b]Search[/b]\n\nSearch by name, extension, size, or directory.",
                        classes="workspace-copy",
                    )
                with TabPane("4 Cleanup", id="cleanup"):
                    yield Static(
                        "[b]Cleanup Center[/b]\n\n"
                        "Review selected items before using the Recycle Bin or permanent deletion.",
                        classes="workspace-copy",
                    )
        yield Static("M0 foundation + M1 filesystem engine · background work never blocks the UI", id="status")
        yield Footer()

    def action_workspace(self, workspace: str) -> None:
        self.query_one(TabbedContent).active = workspace
