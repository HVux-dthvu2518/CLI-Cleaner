"""Report what this machine is actually running before trusting a scan result.

Usage (PowerShell):

    py scripts/check_engine.py            # environment + engine report
    py scripts/check_engine.py --selftest # also scan a temporary tree

The report answers the questions that a failed install hides:

* which Python interpreter is running, and whether it is a virtual environment;
* whether ``disk_explorer`` is the installed package or this source checkout;
* whether the compiled Rust engine imported, and if not, why;
* which traversal policy is active;
* whether long paths are enabled on Windows (a silent source of unreadable files).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("disk_explorer") is None:
    sys.path.insert(0, str(ROOT / "src"))

from disk_explorer import __version__  # noqa: E402
from disk_explorer.analyzer import AnalyzerService  # noqa: E402
from disk_explorer.cache import CacheService, Database, describe as describe_schema  # noqa: E402
from disk_explorer.config import cache_database_path, load_config, user_data_dir  # noqa: E402
from disk_explorer.core import scanner as scanner_module  # noqa: E402
from disk_explorer.core.scanner import FileSystemScanner  # noqa: E402


def _analyzer_fixture() -> Path:
    """Two files in two folders: enough to prove the analyzer collects data."""
    folder = Path(tempfile.mkdtemp(prefix="disk-explorer-analyzer-")) / "fixture"
    (folder / "docs").mkdir(parents=True)
    (folder / "docs" / "a.txt").write_bytes(b"a" * 10)
    (folder / "b.bin").write_bytes(b"b" * 20)
    return folder


_ANALYZER_FIXTURE = _analyzer_fixture()


def _line(label: str, value: object) -> None:
    print(f"{label:<22}{value}")


def report_python() -> list[str]:
    """Print interpreter facts and return any problems worth fixing."""
    problems: list[str] = []
    _line("Interpreter", sys.executable)
    _line("Version", sys.version.split()[0])
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    _line("Virtual env", f"yes ({sys.prefix})" if in_venv else "no (system/global interpreter)")
    if not in_venv:
        problems.append(
            "Not running inside a virtual environment. Create one with "
            "`py -3.12 -m venv .venv` and activate it before installing."
        )
    if os.name == "nt" and "WindowsApps" in sys.executable:
        problems.append(
            "This looks like the Microsoft Store placeholder Python. Use the `py` "
            "launcher (or disable the python.exe app execution alias)."
        )
    _line("Working directory", Path.cwd())
    return problems


def report_package() -> list[str]:
    problems: list[str] = []
    module = __import__("disk_explorer")
    location = Path(getattr(module, "__file__", "")).resolve()
    _line("disk_explorer", __version__)
    _line("Imported from", location)
    if ROOT / "src" not in location.parents:
        _line("Install mode", "site-packages (installed)")
    else:
        _line("Install mode", "source checkout (start.py path)")
        problems.append(
            "The package is being imported from source, not from an installed "
            "distribution. The Rust extension is only built by `pip install -e .` "
            "or `maturin develop`."
        )
    return problems


def report_engine() -> list[str]:
    problems: list[str] = []
    native = scanner_module._rust_scanner
    if native is None:
        _line("Scanner engine", "python (fallback)")
        problems.append(
            "The Rust extension did not import, so scans run in Python. That is "
            "correct but slower. Build it with `python -m pip install -e \".[dev]\"` "
            "on a machine with Rust stable and the MSVC C++ build tools."
        )
        return problems
    _line("Scanner engine", f"rust ({native.ENGINE_NAME} {getattr(native, 'ENGINE_VERSION', '?')})")
    _line("Extension file", getattr(native, "__file__", "?"))
    return problems


def report_traversal() -> list[str]:
    scanner = FileSystemScanner()
    follow_links, reparse_mounts = scanner.traversal_options
    _line("Follow symlinks", "yes (opt-in)" if follow_links else "no (safe default)")
    _line("Follow reparse pts", "yes (opt-in)" if reparse_mounts else "no (safe default)")
    problems: list[str] = []
    if os.name == "nt" and not reparse_mounts:
        problems.append(
            "Reparse-point traversal is off. Windows redirects folders such as "
            "Documents, Downloads, Music, Pictures and Videos with junctions, so "
            "those folders show as skipped links by design. Verify with "
            "`Get-Item $HOME\\Downloads | Format-List Name,Attributes,LinkType,Target`."
        )
    if os.name == "nt":
        problems.extend(_long_path_problems())
    return problems


def _long_path_problems() -> list[str]:
    try:
        import winreg
    except ImportError:  # Not on Windows after all.
        return []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            enabled = winreg.QueryValueEx(key, "LongPathsEnabled")[0]
    except OSError:
        return ["Could not read LongPathsEnabled; long paths may be unavailable."]
    if enabled:
        return []
    return [
        "Long paths (MAX_PATH > 260) are disabled for this machine and this "
        "Python build. Deep paths can fail to enumerate. Enable with "
        "`Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' "
        "-Name LongPathsEnabled -Value 1` (admin) and reboot."
    ]


def report_cache() -> list[str]:
    """Report the hybrid cache: location, schema, contents and health."""
    config = load_config()
    problems: list[str] = []
    if not config.cache_enabled:
        _line("Hybrid cache", "disabled in settings.json")
        problems.append(
            "The hybrid cache is disabled (cache_enabled=false), so every view rescans."
        )
        return problems
    path = cache_database_path(config.cache_path)
    _line("Hybrid cache", path)
    _line("Cache exists", path.exists())
    _line("TTL", f"{config.cache_ttl_minutes} minutes")
    _line("Schema", describe_schema())
    database = Database(path)
    service = CacheService(database, ttl_minutes=config.cache_ttl_minutes)
    try:
        if not service.open():
            return [f"The cache could not be opened: {database.unavailable_reason}"]
        counts = service.repository.counts()
        _line("Cached rows", f"{counts['listings']} listings / {counts['sizes']} sizes")
        if database.recovery_count:
            problems.append(
                "The cache database was damaged and has been rebuilt empty; "
                f"the damaged file was kept as {path.name}.corrupt-*."
            )
    finally:
        service.close()
    _line("User data dir", user_data_dir())
    return problems


def report_analyzer() -> list[str]:
    """Verify the Disk Analyzer works on this machine, on one small tree."""
    scanner = FileSystemScanner()
    service = AnalyzerService(scanner)
    try:
        snapshot = service.analyze_sync(_ANALYZER_FIXTURE, top_files=5, top_folders=3)
    except Exception as error:  # noqa: BLE001 - reported, not raised
        return [f"The Disk Analyzer failed on a small test tree: {error}"]
    _line("Analyzer status", snapshot.status.label)
    _line("Analyzer totals", f"{snapshot.total_files} files / {snapshot.total_size:,} B")
    problems: list[str] = []
    if snapshot.status.value == "failed":
        problems.append("The Disk Analyzer could not analyse a temporary tree; check the errors above.")
    elif snapshot.total_files != 2:
        problems.append(
            f"The Disk Analyzer found {snapshot.total_files} files in a fixture with 2; "
            "file collection may be broken."
        )
    if snapshot.status.value == "partial":
        _line("Analyzer warning", "partial result (unreadable paths or limits)")
    return problems


def report_selftest() -> list[str]:
    """Scan a tiny tree that includes a link, and explain the outcome."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "docs").mkdir()
        (root / "docs" / "a.txt").write_bytes(b"a" * 10)
        (root / "plain.bin").write_bytes(b"b" * 5)
        link_created = True
        try:
            (root / "linked").symlink_to(root / "docs", target_is_directory=True)
        except (OSError, NotImplementedError):
            link_created = False
        scanner = FileSystemScanner()
        listing = scanner.enumerate_directory(root)
        result = scanner.calculate_size(root)
        _line("Self-test engine", scanner.engine_name)
        _line("Listed entries", len(listing.entries))
        _line("Total size", f"{result.total_size} bytes / {result.total_files} files")
        _line("Skipped", result.skipped_summary or "none")
        _line("Errors", len(result.errors))
        for line in result.errors[:5]:
            print(f"  ! {line}")
        problems: list[str] = []
        if link_created and not result.skipped_details:
            problems.append("A test symlink was not reported as skipped; check the scanner.")
        if not link_created:
            _line("Symlink check", "skipped (this environment cannot create links)")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="scan a temporary tree")
    args = parser.parse_args()

    print("== Environment ==")
    problems = report_python()
    print("\n== Package ==")
    problems += report_package()
    print("\n== Engine ==")
    problems += report_engine()
    print("\n== Traversal policy ==")
    problems += report_traversal()
    print("\n== Hybrid cache ==")
    problems += report_cache()
    print("\n== Disk Analyzer ==")
    problems += report_analyzer()
    if args.selftest:
        print("\n== Self-test ==")
        problems += report_selftest()
    print()
    if problems:
        print("Worth fixing:")
        for index, problem in enumerate(problems, start=1):
            print(f"{index}. {problem}")
        return 1
    print("No problems detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
