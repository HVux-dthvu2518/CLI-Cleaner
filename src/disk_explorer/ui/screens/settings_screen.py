from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select


class SettingsScreen(ModalScreen[str | None]):
    """Small settings dialog for screenshot export format."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    CSS = """
    SettingsScreen { align: center middle; background: $background 70%; }
    #settings-dialog { width: 56; height: auto; max-height: 90%; padding: 1 2; border: round $primary; background: $surface; }
    #settings-actions { height: auto; margin-top: 1; align: right middle; }
    #settings-actions Button { margin-left: 1; }
    #format-choice { width: 1fr; }
    """

    def __init__(self, screenshot_format: str) -> None:
        super().__init__()
        self.screenshot_format = screenshot_format

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-dialog"):
            yield Label("Settings", classes="dialog-title")
            yield Label("Screenshot format")
            yield Select(
                [("PNG — recommended", "png"), ("SVG — vector", "svg"), ("JPEG", "jpeg")],
                value=self.screenshot_format,
                allow_blank=False,
                id="format-choice",
            )
            yield Label("PNG/JPEG export needs the optional screenshots extra.", id="format-hint")
            with Horizontal(id="settings-actions"):
                yield Button("Save", variant="primary", id="save-settings")
                yield Button("Cancel", id="cancel-settings")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-settings":
            value = self.query_one(Select).value
            self.dismiss(str(value))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
