"""Compare Rust and Python scanning on an existing directory (warm-cache run)."""

from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("disk_explorer") is None:
    sys.path.insert(0, str(ROOT / "src"))

from disk_explorer.core.scanner import FileSystemScanner, ScanCancellation  # noqa: E402


def average_ms(operation, repeats: int) -> tuple[float, object]:
    durations = []
    last_result = None
    for _ in range(repeats):
        started = time.perf_counter()
        last_result = operation()
        durations.append((time.perf_counter() - started) * 1000)
    return statistics.mean(durations), last_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="directory to scan")
    parser.add_argument("--repeats", type=int, default=3, help="runs per backend (default: 3)")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    root = args.path.expanduser()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    scanner = FileSystemScanner()
    if scanner.engine_name != "rust":
        parser.error("Rust extension not loaded; install with `python -m pip install -e .` first")

    python_listing_ms, python_listing = average_ms(
        lambda: scanner._enumerate_python(root, ScanCancellation()), args.repeats
    )
    rust_listing_ms, rust_listing = average_ms(
        lambda: scanner.enumerate_directory(root, ScanCancellation()), args.repeats
    )
    python_size_ms, python_size = average_ms(
        lambda: scanner._calculate_size_python(root, ScanCancellation()), args.repeats
    )
    rust_size_ms, rust_size = average_ms(
        lambda: scanner.calculate_size(root, ScanCancellation()), args.repeats
    )

    print(f"Path: {root}")
    print(f"Runs: {args.repeats} per backend (warm filesystem cache)")
    print(
        f"Listing: Python {python_listing_ms:.1f} ms; Rust {rust_listing_ms:.1f} ms; "
        f"speedup {python_listing_ms / max(rust_listing_ms, 0.001):.2f}x "
        f"({len(rust_listing.entries):,} entries)"
    )
    print(
        f"Recursive size: Python {python_size_ms:.1f} ms; Rust {rust_size_ms:.1f} ms; "
        f"speedup {python_size_ms / max(rust_size_ms, 0.001):.2f}x "
        f"({rust_size.total_files:,} files, {rust_size.total_size:,} logical bytes)"
    )
    checks = {
        "listing names": sorted(entry.name for entry in python_listing.entries)
        == sorted(entry.name for entry in rust_listing.entries),
        "listing sizes": sorted((entry.name, entry.size) for entry in python_listing.entries)
        == sorted((entry.name, entry.size) for entry in rust_listing.entries),
        "listing flags": sorted(
            (entry.name, entry.is_directory, entry.is_symlink, entry.is_reparse_point)
            for entry in python_listing.entries
        )
        == sorted(
            (entry.name, entry.is_directory, entry.is_symlink, entry.is_reparse_point)
            for entry in rust_listing.entries
        ),
        "listing skips": sorted((item.path.name, item.reason) for item in python_listing.skipped_details)
        == sorted((item.path.name, item.reason) for item in rust_listing.skipped_details),
        "scanned entries": python_listing.scanned_entries == rust_listing.scanned_entries,
        "total size": python_size.total_size == rust_size.total_size,
        "total files": python_size.total_files == rust_size.total_files,
        "total directories": python_size.total_directories == rust_size.total_directories,
        "skip reasons": sorted((item.path.name, item.reason) for item in python_size.skipped_details)
        == sorted((item.path.name, item.reason) for item in rust_size.skipped_details),
        "completeness": python_size.is_complete == rust_size.is_complete,
    }
    failed = [name for name, agrees in checks.items() if not agrees]
    print(f"Result parity: {'yes' if not failed else 'DIFFERS in ' + ', '.join(failed)}")
    if failed:
        print("Inspect permissions and avoid changing the tree during the run.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
