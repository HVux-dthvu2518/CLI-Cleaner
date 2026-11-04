"""Measure hybrid-cache performance against fresh filesystem scans.

Every reported number is checked before it is printed. A "cached" timing is only
reported as a hit when the cache actually returned data; an incomplete scan that
cannot be cached is printed as ``NOT CACHED`` with the reason, never as a speedup.

Workloads:

* directory enumeration - fresh (scan only, no invalidation) vs cache hit
* manual refresh - invalidate + rescan + store, timed separately
* large directory - the folder with the most direct children, found anywhere in
  the tree, timed fresh vs cached
* recursive size - fresh vs cached, including partial results (cached, labelled)
* scan volume for each measured root: entries, skips by reason, and errors

Usage (PowerShell):

    py scripts/benchmark_cache.py --path C:\\Users\\You\\Downloads
    py scripts/benchmark_cache.py --synthetic --entries 5000 --dirs 5

Numbers apply to this machine and filesystem only: cache warmth, antivirus,
storage type and directory shape dominate them.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
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
from disk_explorer.models.scan_result import ScanResult  # noqa: E402


def median_ms(operation, repeats: int) -> tuple[float, object]:
    """Median wall time in ms plus the last value the operation returned."""
    durations: list[float] = []
    value: object = None
    for _ in range(repeats):
        started = time.perf_counter()
        value = operation()
        durations.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(durations), value


def report_hit(label: str, fresh_ms: float, cached_ms: float) -> None:
    speedup = fresh_ms / cached_ms if cached_ms > 0 else float("inf")
    print(f"{label:<26} fresh {fresh_ms:8.2f} ms   cached {cached_ms:8.2f} ms   {speedup:6.2f}x")


def report_not_cached(label: str, fresh_ms: float, reason: str) -> None:
    print(f"{label:<26} fresh {fresh_ms:8.2f} ms   NOT CACHED ({reason})")


def describe_scan(label: str, result: ScanResult) -> None:
    """Print what a scan covered, so skips and errors are visible, not inferred."""
    state = "complete" if result.is_complete else "PARTIAL"
    print(
        f"  {label}: {result.total_files:,} files, {result.total_directories:,} dirs, "
        f"{result.total_size:,} bytes, {result.skipped_entries:,} skipped, "
        f"{len(result.errors)} errors -> {state}"
    )
    if result.skipped_details:
        counts: dict[str, int] = {}
        for item in result.skipped_details:
            counts[item.reason.value] = counts.get(item.reason.value, 0) + 1
        detail = ", ".join(f"{count} {reason}" for reason, count in sorted(counts.items(), key=lambda p: -p[1]))
        print(f"    skipped by design: {detail}")
    for line in result.errors[:3]:
        print(f"    error: {line}")
    if len(result.errors) > 3:
        print(f"    ... and {len(result.errors) - 3:,} more errors")


def largest_folder(scanner: FileSystemScanner, root: Path, sample: int = 400) -> tuple[Path, int]:
    """Folder with the most direct children, searched anywhere under ``root``.

    Only ``sample`` folders are inspected (breadth-first) so a huge tree cannot
    turn the benchmark itself into the slow part. Returns ``(path, child_count)``.
    """
    best_path, best_count = root, -1
    pending = [root]
    inspected = 0
    while pending and inspected < sample:
        folder = pending.pop(0)
        inspected += 1
        try:
            with os.scandir(folder) as children:
                count = 0
                for child in children:
                    count += 1
                    try:
                        if child.is_dir(follow_symlinks=False):
                            pending.append(Path(child.path))
                    except OSError:
                        continue
        except OSError:
            continue
        if count > best_count:
            best_path, best_count = folder, count
    return best_path, max(0, best_count)


def build_synthetic(root: Path, entries: int, dirs: int) -> Path:
    """Create ``dirs`` small folders plus one flat folder with ``entries`` children."""
    for index in range(dirs):
        folder = root / f"folder{index}"
        folder.mkdir(parents=True)
        (folder / "payload.bin").write_bytes(b"x" * 64)
    flat = root / "flat"
    flat.mkdir()
    for item in range(entries):
        (flat / f"file{item:05d}.bin").write_bytes(b"x" * 32)
    return flat


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, help="real directory to measure")
    parser.add_argument("--synthetic", action="store_true", help="use a generated temporary tree")
    parser.add_argument("--entries", type=int, default=2000, help="files in the flat folder (synthetic)")
    parser.add_argument("--dirs", type=int, default=4, help="small folders (synthetic)")
    parser.add_argument("--repeats", type=int, default=5, help="runs per measurement")
    parser.add_argument(
        "--unreadable",
        action="store_true",
        help="synthetic only: add a folder the process cannot read, to exercise partial results",
    )
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
        if args.unreadable:
            protected = root / "protected"
            protected.mkdir()
            (protected / "secret.bin").write_bytes(b"x" * 128)
            try:
                os.chmod(protected, 0o000)
            except OSError:
                print("note: could not make a folder unreadable on this platform; skipping")
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

        # --- enumeration: fresh scan only (no invalidation inside the timing) ---
        service.mark_stale(root)  # start from a miss, outside the measurement
        fresh_listing_ms, fresh_listing = median_ms(
            lambda: scanner.enumerate_directory(root), args.repeats
        )
        service.store_listing(fresh_listing)  # type: ignore[arg-type]
        cached_listing_ms, cached_listing = median_ms(
            lambda: service.cached_listing(root), args.repeats
        )
        if cached_listing is None:
            report_not_cached(
                "Directory enumeration",
                fresh_listing_ms,
                f"cache miss: {service.stats.last_miss_reason}",
            )
        else:
            report_hit("Directory enumeration", fresh_listing_ms, cached_listing_ms)

        # --- manual refresh, timed as the user experiences it ----------------
        def invalidate_and_rescan() -> None:
            service.mark_stale(root)
            service.store_listing(scanner.enumerate_directory(root))

        refresh_ms, _ = median_ms(invalidate_and_rescan, args.repeats)
        print(f"{'Manual refresh':<26} {refresh_ms:8.2f} ms   (invalidate + scan + store)")

        # --- large directory, found anywhere in the tree ---------------------
        if large_listing_root is not None:
            large_root = large_listing_root
            large_count = len(list(large_listing_root.iterdir()))
        else:
            large_root, large_count = largest_folder(scanner, root)
        large_listing = scanner.enumerate_directory(large_root)
        service.store_listing(large_listing)
        service.mark_stale(large_root, tree=True)  # ensure the "fresh" run is fresh
        fresh_large_ms, _ = median_ms(lambda: scanner.enumerate_directory(large_root), args.repeats)
        service.store_listing(scanner.enumerate_directory(large_root))
        decode_ms, large_hit = median_ms(lambda: service.cached_listing(large_root), args.repeats)
        if large_hit is None:
            report_not_cached("Large directory", fresh_large_ms, "cache miss")
        else:
            report_hit("Large directory", fresh_large_ms, decode_ms)
            print(
                f"  largest folder: {large_root} ({large_count:,} direct children, "
                f"{decode_ms / max(1, large_count) * 1000:.1f} us cached per entry)"
            )

        # --- recursive size: fresh vs cached, partial handled honestly -------
        # Only the size row is dropped: the listing row above stays valid and its
        # presence must not influence the size measurement either way.
        service.repository.invalidate(root, include_listings=False)
        fresh_size_ms, fresh_size = median_ms(lambda: scanner.calculate_size(root), args.repeats)
        assert isinstance(fresh_size, ScanResult)
        describe_scan("fresh size scan", fresh_size)

        stored = service.store_size(fresh_size)
        cached_size_ms, cached_size = median_ms(lambda: service.cached_size(root), args.repeats)
        if not stored or cached_size is None:
            reason = "scan is partial and could not be cached" if not stored else "cache miss"
            report_not_cached("Recursive size", fresh_size_ms, f"{reason} - see scan details above")
        else:
            label = "Recursive size" if cached_size.is_complete else "Recursive size (partial)"
            report_hit(label, fresh_size_ms, cached_size_ms)
            if not cached_size.is_complete:
                print(
                    f"  cached partial total excludes {len(cached_size.errors)} unreadable "
                    "path(s); it is returned with is_complete=False"
                )

        counts = service.repository.counts()
        print(f"\nCache rows : {counts['listings']} listings, {counts['sizes']} sizes")
        print(f"Stats      : {service.stats.summary()}")
        print("\nMeasured on this machine and filesystem only; cache warmth, antivirus")
        print("and storage type dominate these numbers.")
    finally:
        service.close()
        if temporary is not None:
            try:  # restore permissions so the temporary tree can be removed
                os.chmod(Path(temporary.name) / "tree" / "protected", 0o700)
            except OSError:
                pass
            temporary.cleanup()
        else:
            shutil.rmtree(cache_root.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
