# Disk Explorer & Cleanup Tool

A performance-first Windows disk explorer built with Python 3.12+ and Textual. The current implementation includes the M0 foundation, a working Explorer for browsing local directories, and the filesystem-engine portion of M1. SQLite caching, analysis, search, and cleanup remain later milestones.

## Run from source

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,screenshots]"
python start.py
```

The `disk-explorer` console command is also installed by `pip install -e .`. Startup does not depend on the current working directory. Application settings and JSONL logs are written under the user's application data directory (`%LOCALAPPDATA%\DiskExplorerCleaner` on Windows; `$XDG_DATA_HOME/disk-explorer-cleaner` or `~/.local/share/disk-explorer-cleaner` elsewhere).

## Current foundations

- Textual shell with a live Explorer plus Analyzer, Search, and Cleanup workspaces; use `1`–`4` and `q` to navigate/quit.
- Browse from the home directory or enter any folder path; use Enter/arrow keys, Backspace, refresh, multi-select, and the details panel.
- Live listing/status includes folder and file counts, recursive-size progress, volume used/free space, and partial/error information.
- Screenshot export defaults to PNG; press F2 to configure PNG, SVG, or JPEG and Ctrl+Shift+S to save under the user's data directory. The `screenshots` extra enables raster PNG/JPEG conversion without requiring a system Cairo install.
- Typed file and scan-result models; unknown folder sizes use `None`.
- Fast, non-recursive `os.scandir` listing and iterative recursive logical-size scanning.
- Symlinks and Windows reparse points are not traversed. Directory identity is tracked to avoid duplicate traversal where the filesystem exposes stable inode IDs.
- Permission, disappearance, and other filesystem errors are recorded; incomplete scans remain explicitly incomplete.
- `ScanService` runs work in a bounded `ThreadPoolExecutor`, reports child folder sizes incrementally, and supports cooperative cancellation.
- User-scoped settings and structured JSON Lines logging; no cache database or destructive operations are implemented yet.

## Filesystem engine example

```python
import asyncio
from disk_explorer.services import ScanService

async def inspect(path: str) -> None:
    async def on_size(entry, result):
        print(entry.name, entry.size, result.is_complete)

    async with ScanService(max_workers=4) as scans:
        listing = await scans.enumerate_directory(path)  # quick first stage
        # Render listing.entries before starting this second stage.
        await scans.calculate_child_sizes(listing.entries, on_update=on_size)
```

The callback receives each updated `FileEntry` and its `ScanResult`. Navigating away should cancel the asyncio task; the service then signals its worker to stop at the next traversal checkpoint.

## Development

```powershell
python -m pip install -e ".[dev]"
pytest
```

The development notes in `AGENTS.md` keep the code lean, filesystem work separate from the UI, and the TUI keyboard-first and responsive. Because this is currently a small, clearly layered package, a generated code knowledge graph would add maintenance rather than useful navigation; the package tree and contracts serve as its map for now.
