# Disk Explorer & Cleanup Tool

A performance-first Windows disk explorer built with Python 3.12+, Textual, and a Rust filesystem engine. Python remains the application/UI layer; the Rust extension accelerates directory enumeration and recursive logical-size traversal and releases the GIL while scanning. A pure-Python `os.scandir` implementation remains available for tests and source checkouts without a compiled extension.

## Windows setup

### Prerequisites

1. Install **Python 3.12+** and **Rust stable** (MSVC toolchain). For example:

   ```powershell
   winget install --id Rustlang.Rustup -e
   rustup toolchain install stable-x86_64-pc-windows-msvc
   rustup default stable-x86_64-pc-windows-msvc
   rustc --version
   cargo --version
   ```

2. Install Visual Studio 2022 Build Tools with **Desktop development with C++** and a Windows SDK. Rust's MSVC target uses these native linker/build tools.

3. From the project directory, create an environment and install the package. PEP 517 invokes Maturin and Cargo to build the Rust extension for the current machine:

   ```powershell
   py -3.12 -m venv .venv
   .venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   python -m pip install -e ".[dev,screenshots]"
   python start.py
   ```

   The `screenshots` extra enables PNG/JPEG export; SVG export does not need it. `.[dev]` installs pytest tooling. The console entry point is also available as `disk-explorer` after installation.

4. Confirm which scanner backend is running: the status line identifies **Rust** when the extension built and loaded, otherwise **Python**. You can check explicitly with:

   ```powershell
   python -c "from disk_explorer import _rust_scanner; print(_rust_scanner.ENGINE_NAME)"
   ```

If the native build fails, verify `rustc`, `cargo`, and the MSVC C++ build tools are available in the same terminal, then retry the editable install. Running directly from a source checkout without installing the package uses the Python fallback; install the package to enable the compiled Rust engine.

## Development and tests

```powershell
python -m pip install -e ".[dev,screenshots]"
pytest
```

For a direct local extension build after dependencies are installed:

```powershell
maturin develop --release
```

To compare the Rust and Python implementations on a real directory (the script reports warm-cache averages and checks result parity):

```powershell
python scripts/benchmark_scanner.py C:\Users\YourName\Downloads --repeats 3
```

Benchmark on the target machine and filesystem; speedups depend on storage, permissions, cache state, and workload. Builds are platform-specific; build/install on each target OS/architecture rather than copying a compiled extension between machines.

## Scanner design

- **Fast first stage:** the engine enumerates only immediate children, so the Explorer can render folders and files before recursive folder-size work finishes.
- **Rust fast path:** Maturin builds `rust/disk_scan` as `disk_explorer._rust_scanner`. PyO3 transfers compact entry/result records and releases Python's GIL for filesystem work, allowing bounded Python worker threads to scan concurrently.
- **Python fallback:** `src/disk_explorer/core/scanner.py` retains equivalent `os.scandir` traversal when the extension is absent. The public `FileSystemScanner` and `ScanService` contracts remain Python APIs.
- **Safety/accuracy:** symbolic links and Windows reparse points are not traversed; inaccessible, disappeared, and cancelled work is not reported as a complete scan. Sizes are logical bytes (`st_size`); hard links may be counted more than once.
- **Bounded concurrency:** `ScanService` caps workers using the configured `max_workers` value and propagates cancellation to either engine.

```python
import asyncio
from disk_explorer.services import ScanService

async def inspect(path: str) -> None:
    async def on_size(entry, result):
        print(entry.name, entry.size, result.is_complete)

    async with ScanService(max_workers=4) as scans:
        listing = await scans.enumerate_directory(path)
        # Render listing.entries before starting the recursive size stage.
        await scans.calculate_child_sizes(listing.entries, on_update=on_size)
```

The callback receives each updated `FileEntry` and its `ScanResult`. Navigating away cancels the asyncio task; the worker checks its cancellation token during traversal.

## Application features

- Browse from the home directory or enter a folder path; use Enter, arrows, Backspace, refresh, and multi-select.
- Live folder/file counts, drive used/free space, size progress, details, and partial/error status.
- Screenshot export defaults to PNG; press F2 to configure PNG, SVG, or JPEG and Ctrl+Shift+S to save under the user's data directory.
- Application settings and structured JSONL logs are stored per-user (`%LOCALAPPDATA%\DiskExplorerCleaner` on Windows; `$XDG_DATA_HOME/disk-explorer-cleaner` or `~/.local/share/disk-explorer-cleaner` elsewhere).
- Analyzer, Search, SQLite caching, and cleanup/deletion are later milestones and are not implemented yet.
