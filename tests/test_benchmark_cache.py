"""The benchmark must never report an unmeasured or bogus speedup.

These tests exist because the first version printed ``26,939x`` for a scan that
had not been cached at all: the "cached" timing measured a failed lookup.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_cache.py"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("benchmark_cache", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def benchmark():
    return _load_benchmark()


def test_not_cached_is_printed_instead_of_a_speedup(benchmark) -> None:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        benchmark.report_not_cached("Recursive size", 2214.40, "scan is partial")

    output = buffer.getvalue()
    assert "NOT CACHED" in output
    assert "scan is partial" in output
    assert "x\n" not in output  # no multiplier is printed at all


def test_hit_reports_the_multiplier(benchmark) -> None:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        benchmark.report_hit("Recursive size", 10.0, 0.05)

    assert "200.00x" in buffer.getvalue()


def test_median_returns_the_last_value(benchmark) -> None:
    counter = {"value": 0}

    def operation() -> int:
        counter["value"] += 1
        return counter["value"]

    duration, value = benchmark.median_ms(operation, 5)

    assert value == 5
    assert duration >= 0.0


def test_largest_folder_searches_beyond_direct_children(benchmark, tmp_path: Path) -> None:
    """The old picker only looked at the root's children and picked a tiny folder."""
    shallow = tmp_path / "shallow"
    shallow.mkdir()
    (shallow / "one.txt").write_bytes(b"1")
    deep = tmp_path / "deep" / "nested" / "busiest"
    deep.mkdir(parents=True)
    for index in range(40):
        (deep / f"file{index}.bin").write_bytes(b"x")
    (tmp_path / "small").mkdir()

    from disk_explorer.core.scanner import FileSystemScanner

    path, count = benchmark.largest_folder(FileSystemScanner(), tmp_path)

    assert path == deep
    assert count == 40


def test_scan_details_make_skips_and_errors_visible(benchmark, tmp_path: Path) -> None:
    from disk_explorer.models.scan_result import ScanResult, SkippedEntry
    from disk_explorer.models.skip_reason import SkipReason

    partial = ScanResult(
        root=tmp_path,
        total_size=100,
        total_files=2,
        total_directories=3,
        skipped_entries=2,
        errors=[f"{tmp_path / 'protected'}: Access is denied"],
        is_complete=False,
        skipped_details=[
            SkippedEntry(path=tmp_path / "junction", reason=SkipReason.REPARSE_POINT),
            SkippedEntry(path=tmp_path / "link", reason=SkipReason.REPARSE_POINT),
        ],
    )
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        benchmark.describe_scan("fresh size scan", partial)

    output = buffer.getvalue()
    assert "-> PARTIAL" in output
    assert "2 reparse point" in output
    assert "error:" in output and "Access is denied" in output


def _run_benchmark(benchmark, monkeypatch, *arguments: str) -> str:
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["benchmark_cache.py", *arguments])
    with redirect_stdout(buffer):
        assert benchmark.main() == 0
    return buffer.getvalue()


@pytest.mark.skipif(not hasattr(__import__("os"), "chmod"), reason="needs chmod")
def test_complete_synthetic_tree_reports_a_real_hit(benchmark, monkeypatch) -> None:
    output = _run_benchmark(benchmark, monkeypatch, "--synthetic", "--entries", "60", "--dirs", "2", "--repeats", "1")

    assert "fresh size scan" in output and "-> complete" in output
    assert "Recursive size " in output and "x" in output
    assert "NOT CACHED" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions drive this case")
def test_unreadable_folder_is_reported_as_a_partial_cached_result(benchmark, monkeypatch) -> None:
    output = _run_benchmark(
        benchmark,
        monkeypatch,
        "--synthetic",
        "--entries",
        "40",
        "--dirs",
        "1",
        "--repeats",
        "1",
        "--unreadable",
    )

    assert "-> PARTIAL" in output
    assert "error:" in output and "Permission denied" in output
    # Partial totals are now cached, and labelled as partial rather than hidden.
    assert "Recursive size (partial)" in output
    assert "is_complete=False" in output
