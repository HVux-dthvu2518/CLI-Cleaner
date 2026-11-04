"""Measure hybrid-cache performance against fresh filesystem scans.

Reports medians over repeated runs for four workloads:

* fresh enumeration (no cache row)
* cache hit (same directory, row valid)
* invalidation + refresh (manual refresh path)
* recursive size: fresh vs cached
* large directory (many entries per directory)

Usage (PowerShell):

    py scripts/benchmark_cache.py --path C:\\Users\\You\\Downloads
    py scripts/benchmark_cache.py --synthetic --entries 5000 --dirs 5

Numbers are only printed for this machine and filesystem. Do not quote them as
general speedups: warm cache state, antivirus, storage type and directory shape
all dominate. ``--synthetic`` uses a temporary tree so results are comparable
between machines; ``--path`` measures a real folder.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("disk_explorer") is None:
    sys.path.insert(0, str(ROOT / "src"))

from disk_explorer.cache.cache_service import CacheService  # noqa: E402
from disk_explorer.cache.database import Database  # noqa: E402
from disk_explorer.core.scanner import FileSystemScanner  # noqa: E402


def timed(operation) -> float:
    started = time.perf_counter()
    operation()
    return (time.perf_counter() - started) * 1000.0


def median(operation, repeats: int) -> float:
    return statistics.median(timed(operation) for _ in range(repeats))


def build_synthetic(root: Path, entries: int, dirs: int) -> Path:
    """Create ``dirs`` sub-folders plus one flat folder with ``entries`` children.

    Returns the flat folder, which is the large-directory case: one directory with
    thousands of direct children, so enumeration and decode cost are both visible
    instead of being averaged away.
    """
    for index in range(dirs):
        folder = root / f"folder{index}"
        folder.mkdir(parents=True)
        (folder / "payload.bin").write_bytes(b"x" * 64)
    flat = root / "flat"
    flat.mkdir()
    for item in range(entries):
        (flat / f"file{item:05d}.bin").write_bytes(b"x" * 32)
    return flat


def report(label: str, fresh_ms: float, cached_ms: float) -> None:
    speedup = fresh_ms / cached_ms if cached_ms > 0 else float("inf")
    print(f"{label:<26} fresh {fresh_ms:8.2f} ms   cached {cached_ms:8.2f} ms   {speedup:6.2f}x")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, help="real directory to measure")
    parser.add_argument("--synthetic", action="store_true", help="use a generated temporary tree")
    parser.add_argument("--entries", type=int, default=2000, help="files per folder (synthetic)")
    parser.add_argument("--dirs", type=int, default=4, help="folders (synthetic)")
    parser.add_argument("--repeats", type=int, default=5, help="runs per measurement")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    temporary: tempfile.TemporaryDirectory[str] | None = None
    large_listing_root: Path | None = None
    if args.synthetic:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name) / "tree"
        root.mkdir()
        large_listing_root = build_synthetic(root, args.entries, args.dirs)
        cache_root = Path(temporary.name) / "cache"
    elif args.path:
        root = args.path.expanduser().resolve()
        if not root.is_dir():
            parser.error(f"not a directory: {root}")
        cache_root = Path(tempfile.mkdtemp()) / "cache"
    else:
        parser.error("pass --path <dir> or --synthetic")

    scanner = FileSystemScanner()
    service = CacheService(Database(cache_root / "scan.sqlite3"), ttl_minutes=30)
    if not service.open():
        parser.error("could not open the cache database")
    service.repository.ttl_seconds = 1800

    try:
        print(f"Path       : {root}")
        print(f"Entries    : {sum(1 for _ in root.rglob('*')):,}")
        print(f"Engine     : {scanner.engine_name}")
        print(f"Repeats    : {args.repeats} (median reported)\n")

        # Fresh enumeration: invalidate before every run so nothing is cached.
        def fresh_enumeration() -> None:
            service.mark_stale(root)
            scanner.enumerate_directory(root)

        fresh_listing_ms = median(fresh_enumeration, args.repeats)

        # Cache hit: one store, then measure reads only.
        service.store_listing(scanner.enumerate_directory(root))
        cached_listing_ms = median(lambda: service.cached_listing(root), args.repeats)
        report("Directory enumeration", fresh_listing_ms, cached_listing_ms)

        # Invalidation + refresh: the manual refresh path (F5/r).
        def invalidate_and_rescan() -> None:
            service.mark_stale(root)
            service.store_listing(scanner.enumerate_directory(root))

        refresh_ms = median(invalidate_and_rescan, args.repeats)
        print(f"{'Invalidate + refresh':<26} {refresh_ms:8.2f} ms   (miss + scan + store)")

        # Large listing: decode cost scales with the number of direct children.
        large_root = large_listing_root or max(
            (entry.path for entry in scanner.enumerate_directory(root).entries if entry.is_directory),
            key=lambda path: len(list(path.iterdir())),
            default=root,
        )
        large_listing = scanner.enumerate_directory(large_root)
        service.store_listing(large_listing)
        fresh_large_ms = median(
            lambda: scanner.enumerate_directory(large_root), args.repeats
        )
        decode_ms = median(lambda: service.cached_listing(large_root), args.repeats)
        report("Large directory", fresh_large_ms, decode_ms)
        print(
            f"{'  entries / per-entry':<26} {len(large_listing.entries):,} entries, "
            f"{decode_ms / max(1, len(large_listing.entries)) * 1000:.1f} us cached per entry"
        )

        # Recursive sizes: fresh walk vs cached row.
        def fresh_size() -> None:
            service.repository.invalidate(root)
            scanner.calculate_size(root)

        fresh_size_ms = median(fresh_size, args.repeats)
        service.store_size(scanner.calculate_size(root))
        cached_size_ms = median(lambda: service.cached_size(root), args.repeats)
        report("Recursive size", fresh_size_ms, cached_size_ms)

        counts = service.repository.counts()
        print(f"\nCache rows : {counts['listings']} listings, {counts['sizes']} sizes")
        print(f"Stats      : {service.stats.summary()}")
        print("\nMeasured on this machine and filesystem only; cache warmth, antivirus")
        print("and storage type dominate these numbers.")
    finally:
        service.close()
        if temporary is not None:
            temporary.cleanup()
        else:
            shutil.rmtree(cache_root.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
