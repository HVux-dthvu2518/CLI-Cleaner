"""Read-only report showing why entries were skipped."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label
from textual.widgets.data_table import RowKey

from disk_explorer.models.skip_reason import SKIP_REASON_DETAIL, SkipReason
from disk_explorer.models.scan_result import SkippedEntry

#: The SkipReason to explain when nothing was actually skipped.
_NO_SKIP_DETAIL = (
    "No entries were skipped. Every child of the scanned folder was counted. "
    "Folders that could not be read are listed as errors in the status line instead."
)


class SkippedEntriesScreen(ModalScreen[None]):
    """Explain each skipped entry instead of only reporting a count.

    This screen never modifies anything; it exists so a skipped folder is a
    visible, explained decision rather than a silently missing row.
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]
    CSS = """
    SkippedEntriesScreen { align: center middle; background: $background 70%; }
    #skip-dialog {
        width: 96;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #skip-summary { color: $text-muted; margin-bottom: 1; }
    #skip-table { height: auto; max-height: 16; min-height: 4; }
    #skip-why { color: $text-muted; margin-top: 1; }
    #skip-actions { height: auto; margin-top: 1; align: right middle; }
    """

    def __init__(
        self,
        root: Path,
        skipped: list[SkippedEntry],
        unreadable: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.root = root
        self.skipped = skipped
        #: Paths that could not be read (permission denied, vanished, locked).
        self.unreadable = list(dict.fromkeys(unreadable or []))

    def compose(self) -> ComposeResult:
        counts = Counter(item.reason for item in self.skipped)
        parts = [
            f"{count} {reason.value}"
            for reason, count in sorted(counts.items(), key=lambda pair: -pair[1])
        ]
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} unreadable")
        if parts:
            headline = f"Under {self.root}: " + "  ·  ".join(parts)
        else:
            headline = f"Nothing was skipped under {self.root}"
        with Vertical(id="skip-dialog"):
            yield Label("Skipped entries", classes="dialog-title")
            yield Label(headline, id="skip-summary")
            yield DataTable(id="skip-table", cursor_type="row", zebra_stripes=True)
            yield Label(self._explanation(counts), id="skip-why")
            with Horizontal(id="skip-actions"):
                yield Button("Close", variant="primary", id="close-skips")

    def on_mount(self) -> None:
        table = self.query_one("#skip-table", DataTable)
        table.add_column("Reason", key="reason", width=20)
        table.add_column("Path", key="path")
        rows = [(item.reason.value, str(item.path)) for item in self.skipped]
        rows.extend(("unreadable", line) for line in self.unreadable)
        for index, (reason, path) in enumerate(rows):
            table.add_row(reason, path, key=RowKey(str(index)))

    def _explanation(self, counts: Counter[SkipReason]) -> str:
        if not counts and not self.unreadable:
            return _NO_SKIP_DETAIL
        lines = [f"{reason.value}: {SKIP_REASON_DETAIL[reason]}" for reason in counts]
        if self.unreadable:
            lines.append(
                "unreadable: permission denied, a locked file or a path that vanished. These are "
                "reported as errors and make the total partial - never a successful zero-byte scan."
            )
        lines.append(
            "Link and reparse-point traversal is off by default; enable it in settings.json "
            "(follow_links / reparse_mounts) if you accept the loop and duplicate-counting risk."
        )
        return "\n".join(lines)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-skips":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
