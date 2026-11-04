"""Measure Disk Analyzer workloads honestly.

Reports what was measured, how much of the tree it covered, and whether the cache
served the folder totals. A run that cannot complete prints its errors instead of
a number disguised as a success.

Usage (PowerShell):

    py scripts/benchmark_analyzer.py --synthetic --files 20000 --folders 40
    py scripts/benchmark_analyzer.py --path C:\\Users\\You\\Downloads

Measured on this machine and filesystem only.
"""

from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("disk_explorer") is None:
    sys.path.insert(0, str(ROOT / "src"))

from disk_explorer.analyzer import AnalysisFilters, AnalyzerService, SnapshotStatus  # noqa: E402
from disk_explorer.cache.cache_service import CacheService  # noqa: E402
from disk_explorer.cache.database import Database  # noqa: E402
from disk_explorer.core.scanner import FileSystemScanner  # noqa: E402


def build_synthetic(root: Path, files: int, folders: int) -> None:
    """Spread ``files`` files over ``folders`` folders with mixed extensions."""
    extensions = [".bin", ".jpg", ".mp4", ".txt", ""]
    per_folder = max(1, files // max(1, folders))
    for folder_index in range(folders):
        folder = root / f"folder{folder_index:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        for item in range(per_folder):
            extension = extensions[item % len(extensions)]
            payload = (item % 64) * 32
            name = f"file{item:05d}{extension}"
            (folder / name).write_bytes(b"x" * payload)


def timed_run(service: AnalyzerService, root: Path, **kwargs) -> tuple[float, object]:
    started = time.perf_counter()
    snapshot = service.analyze_sync(root, **kwargs)
    return (time.perf_counter() - started) * 1000.0, snapshot


def describe(label: str, snapshot, duration_ms: float) -> None:
    state = snapshot.status.value.upper()
    print(
        f"{label:<34} {duration_ms:9.2f} ms   {state}  ·  "
        f"{snapshot.total_files:,} files · {snapshot.total_directories:,} folders · "
        f"{snapshot.total_size:,} B"
    )
    if snapshot.largest_files:
        print(
            f"{'':<34} top file: {snapshot.largest_files[0].name} "
            f"({snapshot.largest_files[0].size:,} B) · "
            f"top folder: {snapshot.largest_folders[0].name} "
            f"({snapshot.largest_folders[0].size:,} B)"
        )
    if snapshot.skipped_entries:
        reasons = ", ".join(f"{count} {reason}" for reason, count in snapshot.skipped_by_reason.items())
        print(f"{'':<34} skipped: {snapshot.skipped_entries:,} ({reasons})")
    for problem in snapshot.errors[:3]:
        print(f"{'':<34} ! {problem}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, help="real directory to analyse")
    parser.add_argument("--synthetic", action="store_true", help="generate a temporary tree")
    parser.add_argument("--files", type=int, default=20000, help="files to generate")
    parser.add_argument("--folders", type=int, default=40, help="folders to generate")
    parser.add_argument("--repeats", type=int, default=3, help="runs per measurement")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.synthetic:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name) / "tree"
        root.mkdir()
        build_synthetic(root, args.files, args.folders)
        cache_root = Path(temporary.name) / "cache"
    elif args.path:
        root = args.path.expanduser().resolve()
        if not root.is_dir():
            parser.error(f"not a directory: {root}")
        cache_root = Path(tempfile.mkdtemp()) / "cache"
    else:
        parser.error("pass --path <dir> or --synthetic")

    cache = CacheService(Database(cache_root / "scan.sqlite3"), ttl_minutes=30)
    if not cache.open():
        parser.error("could not open the cache database")
    service = AnalyzerService(FileSystemScanner(), cache)

    try:
        print(f"Path     : {root}")
        print(f"Engine   : {service.scanner.engine_name}")
        print(f"Repeats  : {args.repeats} (median reported)\n")

        # Cold: nothing cached yet.
        cold_runs = []
        snapshot = None
        for _ in range(args.repeats):
            cache.clear()
            duration, snapshot = timed_run(service, root, top_files=100, top_folders=50)
            cold_runs.append(duration)
        describe("Cold analysis (empty cache)", snapshot, statistics.median(cold_runs))

        # Warm: valid size rows in the cache.
        warm_runs = []
        for _ in range(args.repeats):
            duration, snapshot = timed_run(service, root, top_files=100, top_folders=50)
            warm_runs.append(duration)
        describe("Warm analysis (cache hit)", snapshot, statistics.median(warm_runs))

        cold, warm = statistics.median(cold_runs), statistics.median(warm_runs)
        speedup = cold / warm if warm > 0 else float("inf")
        print(f"\n{'Cache effect':<34} {speedup:9.2f}x  (cold / warm)")

        # Force refresh ignores the cache.
        refresh_runs = []
        for _ in range(args.repeats):
            duration, snapshot = timed_run(service, root, force_refresh=True)
            refresh_runs.append(duration)
        describe("Forced refresh", snapshot, statistics.median(refresh_runs))

        # Filters should reduce the reported set without changing what was read.
        filtered_runs = []
        for _ in range(args.repeats):
            duration, snapshot = timed_run(
                service, root, filters=AnalysisFilters(min_size=1024), top_files=100
            )
            filtered_runs.append(duration)
        describe("With min-size filter (>=1 KB)", snapshot, statistics.median(filtered_runs))

        counts = cache.repository.counts()
        print(f"\nCache rows : {counts['listings']} listings, {counts['sizes']} sizes")
        print(f"Analyzer   : top {len(snapshot.largest_files)} files, "
              f"top {len(snapshot.largest_folders)} folders, "
              f"{len(snapshot.extensions.entries)} extensions")
        if snapshot.status is not SnapshotStatus.COMPLETE:
            print(f"NOTE: this run was {snapshot.status.value}; totals exclude what could not be read.")
        print("\nMeasured on this machine and filesystem only; storage type, antivirus and")
        print("cache warmth dominate these numbers.")
    finally:
        cache.close()
        if temporary is not None:
            temporary.cleanup()
        else:
            import shutil

            shutil.rmtree(cache_root.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
