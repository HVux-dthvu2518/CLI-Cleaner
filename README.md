# Disk Explorer & Cleanup Tool

A performance-first Windows disk explorer built with Python 3.11+, Textual, and a Rust filesystem engine. Python remains the application/UI layer; the Rust extension accelerates directory enumeration and recursive logical-size traversal and releases the GIL while scanning. A pure-Python `os.scandir` implementation remains available for tests and source checkouts without a compiled extension.

## Windows setup

### Prerequisites

1. Install **Python 3.11+** (3.12+ recommended) and **Rust stable 1.80 or newer** (MSVC toolchain). The crate's `rust-version` is 1.80 because PyO3 0.23 requires it; older toolchains fail the build with confusing errors. For example:

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
   .\.venv\Scripts\Activate.ps1
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

### Diagnose the environment before trusting a scan

```powershell
py scripts/check_engine.py --selftest
```

The report prints the interpreter, whether it is a virtual environment, whether `disk_explorer` came from this checkout or from site-packages, which engine actually imported, the active traversal policy, and self-tests a temporary tree. It exits non-zero when something needs fixing, with the exact command to fix it.

### Common install failure: the Rust build stops at `windows_by_handle`

Earlier revisions called `metadata.volume_serial_number()` and `metadata.file_index()`, which are gated behind Rust's **unstable** `windows_by_handle` feature (E0658). On stable Rust the extension never built, so `from disk_explorer import _rust_scanner` failed with `ImportError` even though the app still started on the Python fallback. The engine now uses inode identity on Unix and canonical paths on Windows, so it builds on stable Rust. `tests/test_rust_source.py` guards against that regression.

## Development and tests

```powershell
python -m pip install -e ".[dev,screenshots]"
pytest
```

Cache-aware tests use a temporary database and never touch your real cache;
`tests/test_cache.py` and `tests/test_cache_integration.py` cover persistence,
validation, invalidation, cancellation, concurrency and corruption recovery.

The suite runs without a Rust toolchain: engine contract tests validate the Python engine as the reference and automatically compare the Rust engine when it is importable (`tests/test_engine_contract.py`), plus source-level guards in `tests/test_rust_source.py`.

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
- **Explained exclusions:** every entry that is not counted carries a reason. Symlinks, reparse points, directories already visited in the same scan, and non-regular files (devices, pipes) are recorded as typed `SkippedEntry` values instead of being silently dropped. Unreadable paths stay errors and make the result partial.
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

## Why some folders are reported as skipped

Skips are deliberate, and the app now names the reason instead of hiding it:

| Reason | What it means |
| --- | --- |
| `symlink` | A symbolic link. Not followed by default, so its target is not counted through it. |
| `reparse point` | A Windows junction or mount point. Not followed by default. |
| `already visited` | The same directory reached twice in one scan, e.g. through a link when traversal is enabled. |
| `not a regular file` | A device, pipe or socket with no meaningful logical size. |

Press **`s`** in the Explorer to open a read-only report listing every skipped path and the reason, plus any path that could not be read. The details panel and status line show the same summary, for example `Rust scan partial · 1 error(s) · 3 skipped (2 reparse point(s), 1 symlink(s)) · press s for details`.

**Windows user folders are the usual cause.** Windows redirects `Documents`, `Downloads`, `Music`, `Pictures`, `Videos` and `AppData\Local` with junctions. Check a folder yourself:

```powershell
Get-Item $HOME\Downloads | Format-List Name,Attributes,LinkType,Target
```

If `LinkType` reports a junction, the folder is a reparse point and its contents are excluded from recursive totals by design. Open the folder directly to browse and size its contents; the policy only affects recursion.

If you accept the risk of loops and duplicate counting, opt in through `settings.json` in the user data directory:

```json
{ "follow_links": false, "reparse_mounts": true }
```

`follow_links` follows symbolic links, `reparse_mounts` follows non-symlink reparse points such as junctions. Both default to `false`. With traversal enabled, a directory reached twice is skipped once as `already visited`, so cycles cannot loop forever.

Skipped entries are never treated as errors, and errors are never treated as skips: a skipped link keeps the scan complete, while a permission failure makes it partial.

## SQLite hybrid cache

Scans are cached per user so returning to a folder is instant, without ever
showing stale data as fresh. The cache never holds user data and can be deleted
at any time.

```powershell
# Where it lives
$env:LOCALAPPDATA\DiskExplorerCleaner\cache\scan-cache.sqlite3

# Disable it, change the TTL (minutes), or relocate it in settings.json
{ "cache_enabled": true, "cache_ttl_minutes": 30, "cache_path": "" }
```

**Separate tables, separate rules.** `listings` (immediate children) are validated
by the directory's modification time and the TTL. `sizes` (recursive totals) are
validated by TTL, the root mtime, and the recorded mtimes of child listings from
the same scan — a directory's own mtime cannot see changes deeper in the tree,
which is exactly why the TTL exists. Full column reference and failure matrix:
[docs/cache-schema.md](docs/cache-schema.md).

**Cached data carries its own truth.** Every size row stores `is_complete` and its
error list, so a partial total comes back as `is_complete=False` and is labelled
`Cached·part` in the table instead of being presented as a complete size.

- Directory *listings* stay strict: a partial listing would silently hide
  children, and a miss costs one `scandir`.
- Recursive *sizes* may be partial, because that is the expensive case — a 2.2 s
  walk over a 411k-entry tree where two protected folders make the result
  partial. It is cached only together with its error list; a "partial" result
  with no explanation, or a cancelled walk, is never stored.
- An unreadable folder is never cached as a complete empty scan, and a partial
  scan that measured nothing shows `—` (unknown), never `0 B`.

**Refresh means refresh.** `r` (or the Refresh button) drops the cached rows for
the current folder and rescans. Startup housekeeping prunes rows past their TTL.

**Failures fall back to scanning.** A corrupted database is renamed to
`scan-cache.sqlite3.corrupt-<timestamp>` and rebuilt; an unwritable directory, a
locked database, or a newer schema version simply disables the cache for the
session. The application never fails because of the cache.

Scoping: list and recursive-size data can be cached **only if** the directory mtime
is readable, which needs a directory the app can `stat`. Network shares with
unreliable timestamps will mostly cache-miss rather than serve stale data — an
intentional trade.

Schema history: v1 stored complete scans only; v2 adds `is_complete` and `errors`
to `sizes` so partial totals can be cached and labelled. Upgrading is automatic
and keeps existing rows ([docs/cache-schema.md](docs/cache-schema.md#migrations)).

### Measured behaviour

`scripts/benchmark_cache.py` reports medians for this machine and filesystem;
cache warmth, antivirus and storage type dominate the numbers, so do not read them
as general speedups. On the development sandbox (Linux, Python engine, warm cache):

| Workload | Fresh | Cache hit | Notes |
| --- | --- | --- | --- |
| Recursive size, 1,200-file synthetic tree (Python engine) | 6.22 ms | 0.05 ms | the case caching targets |
| Large directory, 1,200 direct children | 8.47 ms | 4.14 ms | ~3 µs per entry to decode |
| Directory enumeration, 5 entries | 0.07 ms | 0.09 ms | **slower than the syscall** |
| Manual refresh (invalidate + scan + store) | — | 0.18 ms | one `scandir` plus a commit |

Reproduce on Windows with your own folders:

```powershell
py scripts/benchmark_cache.py --synthetic --entries 2000 --dirs 4 --repeats 5
py scripts/benchmark_cache.py --path $HOME\Downloads --repeats 3
py scripts/benchmark_cache.py --synthetic --entries 500 --dirs 2 --unreadable   # partial-result path
```

The harness refuses to report a speedup for a lookup that returned nothing: an
uncacheable scan prints `NOT CACHED (reason)`, and each measured scan prints its
file/directory/skip/error counts so a partial result is visible rather than
inferred.

The honest summary: the cache buys nothing for a single small local `scandir`
(fractions of a millisecond — sometimes *slower* than the syscall, as the table
shows), and a lot for recursive sizes and antivirus-scanned or network storage. It
is not a substitute for the Rust engine on enumeration cost.

## Disk Analyzer

Press **`2`** for the Analyzer workspace. It answers four questions: which files are
largest, which folders are largest, which file types consume the most space, and
how far the scan actually got.

```powershell
# Analyse a folder (nothing is scanned until you ask)
#  2        open the Analyzer tab
#  Analyse  run on the path in the toolbar
#  Refresh  ignore cached listings and sizes (force a real rescan)
#  Export   write three CSV reports to the user data directory
#  Sort ↑↓  toggle largest/smallest first
#  enter    jump to that file's folder (or that folder) in the Explorer
```

### Seeing it instead of reading it

The **Visuals** tab (the first tab) answers "what is big" with shapes rather than a
list of numbers:

* a **treemap** of the analysed folder's direct children, where box area is
  proportional to bytes, so the biggest folder is visibly the biggest;
* a **ring chart** of bytes per file type, with the share next to each label;
* **bars** for the largest folders and largest files;
* a **drive used / analysed** bar, keeping allocated space separate from logical
  bytes.

Everything is drawn with standard library characters - no charting dependency - in
`analyzer/visuals.py`, which has no Textual imports and is unit-tested for honesty
rather than for pixels:

* the treemap draws **only mutually exclusive folders** (the analysed folder's own
  children). A "largest folders" list mixes a folder with its ancestors, so drawing
  that list would double count;
* every measured folder gets at least one cell, and anything not drawn is counted in
  a `+N smaller (size) not drawn` line - the picture never quietly crops;
* a folder whose size is unknown is **not drawn at all**, rather than drawn as zero;
* an incomplete scan is labelled under the charts, so a partial picture cannot be
  read as a complete one.

Filters: minimum size in bytes and an extension such as `.mp4` (case-insensitive).

**Nothing is scanned automatically.** The Analyzer starts empty; it never walks a
drive on open, and it never elevates privileges.

**No double counting.** Folder sizes are recursive totals, so they are *never*
summed. The analysed total is the sum of file sizes only, which is also why
`total_size` equals the sum of the largest-files list when no filter is applied.

**Unknown is not zero.** A folder whose size could not be measured shows `—` and
exports as a blank CSV cell; a partial folder is marked `Partial` and its totals
exclude unreadable paths. The scan state is always visible: `Complete`, `Partial`,
`Cancelled` or `Failed`, with skip counts by reason (reparse points, symlinks) and
unreadable paths listed.

**Bounded work.** The walk is breadth-first with configurable limits
(`AnalyzerLimits`): 50,000 folders analysed in detail, 64 levels deep. Hitting a
limit marks the report `Partial` with an explanation instead of silently
truncating it.

**Folder totals cost nothing extra.** Every directory is enumerated exactly once,
and each folder's recursive total is the bottom-up sum of the direct file bytes
already collected. Asking the scanner for a recursive size per folder instead
re-walks the subtree, which is O(folders × tree) - on a real 16,652-folder tree
that was the difference between 15 s and well under a second.

**A cache hit means "did not touch the disk".** Directory listings are cached as
well as recursive sizes, and the walk reads them in batches (one query per sweep of
256 folders). Only folders whose listing is missing or stale are enumerated, and
only folders whose total actually changed are written back. The status line reports
how many folders came from the cache, e.g. `94/94 folders cached`.

**Reusable models.** `AnalysisSnapshot`, `FileRecord`, `FolderRecord` and
`ExtensionSummary` are plain data with no Textual or SQLite imports (enforced by a
test). M4 Search and M5 Cleanup should consume them instead of walking the
filesystem again.

CSV export writes `disk-analysis-files.csv`, `disk-analysis-folders.csv` and
`disk-analysis-extensions.csv` as UTF-8 with a BOM (so Excel renders non-ASCII
paths correctly), and every file ends with a metadata block stating the scan
status, so an exported partial report cannot be mistaken for a complete one.

### Measured behaviour

`scripts/benchmark_analyzer.py`, Linux sandbox, Python engine, medians:

| Workload (Linux sandbox, Python engine, medians) | Time | Notes |
| --- | --- | --- |
| Cold analysis, `/usr/share` (3,702 files, 1,171 folders) | 172.65 ms | empty cache, 420 symlinks skipped |
| Warm analysis, `/usr/share` | 75.98 ms | **2.27×** from the cache |
| Forced refresh, `/usr/share` | 92.67 ms | cache bypassed |
| Cold analysis, 1,617 folders (synthetic, folder-heavy) | 234.15 ms | empty cache |
| Warm analysis, 1,617 folders | 90.67 ms | **2.58×** |
| Cold/warm, `/usr/lib/python3.11` (94 folders) | 40.05 → 13.95 ms | **2.87×** |
| Folder scaling, 250 → 2,000 folders | 24.4 → 187 ms | near-linear (was 43.7 → 364 ms) |

The folder-scaling row is the regression guard: before the fix, every folder's
recursive size re-walked its subtree, so cost grew with folders × tree.

Reproduce on Windows:

```powershell
py scripts/benchmark_analyzer.py --synthetic --files 20000 --folders 40 --repeats 3
py scripts/benchmark_analyzer.py --folder-heavy --folders 2000 --repeats 3
py scripts/benchmark_analyzer.py --path $HOME\Downloads --repeats 3
```

## Engine contract

Both engines are reached through `FileSystemScanner` and return the same records, so a missing or broken extension cannot change scan semantics:

```python
_rust_scanner.enumerate_directory(path, token, follow_links=False, reparse_mounts=False)
# -> (entries, scanned, errors, is_complete, cancelled, skip_records)
#    entries: (name, is_directory, size|None, modified_ns|None, extension,
#              is_symlink, is_reparse_point)

_rust_scanner.calculate_size(path, token, follow_links=False, reparse_mounts=False)
# -> (total_size, total_files, total_directories, scanned, errors,
#     is_complete, cancelled, skip_records)

# skip_records: [(absolute_path, reason), ...] where reason is a SkipReason value
```

`FileSystemScanner` converts those tuples into `FileEntry` / `DirectoryListing` / `ScanResult` models and maps `skip_records` into `SkippedEntry(path, reason)`. Anything new the engine reports must be added to `SkipReason` first, or it will be ignored rather than shown to the user.

## Application features

- Browse from the home directory or enter a folder path; use Enter, arrows, Backspace, refresh, and multi-select.
- Press **`s`** for the skipped-entries report explaining every excluded or unreadable path.
- Live folder/file counts, drive used/free space, size progress, details, and partial/error status.
- Screenshot export defaults to PNG; press F2 to configure PNG, SVG, or JPEG and Ctrl+Shift+S to save under the user's data directory.
- Application settings and structured JSONL logs are stored per-user (`%LOCALAPPDATA%\DiskExplorerCleaner` on Windows; `$XDG_DATA_HOME/disk-explorer-cleaner` or `~/.local/share/disk-explorer-cleaner` elsewhere).
- SQLite hybrid caching is implemented (`src/disk_explorer/cache/`): directory listings and recursive folder sizes are cached separately, validated by metadata + TTL, and invalidated on manual refresh. See [docs/cache-schema.md](docs/cache-schema.md).
- Disk Analyzer is implemented (`src/disk_explorer/analyzer/`): largest files, largest folders, extension statistics, storage overview, filters and CSV export, built on the scanner and the hybrid cache. Folder totals come from a single walk, listings and totals are both cached, and the Visuals tab renders a treemap, a ring chart and bars with no third-party charting dependency.
- Explorer sorting: press **`o`** to step through Name → Size → Modified → Type, **`shift+o`** to reverse, or click a column header. Folders stay grouped above files in every order, unknown sizes sort last in both directions, and sorting by size keeps re-ordering rows as folder sizes arrive.
- Search and cleanup/deletion are later milestones and are not implemented yet.
