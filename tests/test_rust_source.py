"""Guards for the Rust engine source: the failures this repo already hit.

These are source-level checks (no Rust toolchain required) for the two problems
that broke real installs:

1. ``Metadata::volume_serial_number()`` and ``Metadata::file_index()`` are gated
   behind the unstable ``windows_by_handle`` feature, so the crate failed to
   compile on stable Rust 1.75-1.79 (E0658) and the extension was never built.
2. The Python/Rust call signature drifted, so even a successful build could be
   unusable from ``FileSystemScanner``.

A compiled-extension test in ``tests/test_engine_contract.py`` covers the rest
when a build is actually available.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB_RS = ROOT / "rust" / "disk_scan" / "src" / "lib.rs"
CARGO_TOML = ROOT / "rust" / "disk_scan" / "Cargo.toml"


def test_rust_source_exists() -> None:
    assert LIB_RS.is_file(), "the Rust engine source is missing"
    assert CARGO_TOML.is_file()


@pytest.mark.parametrize(
    "unstable_api",
    ["volume_serial_number", "file_index()", "windows_by_handle"],
)
def test_no_unstable_windows_apis(unstable_api: str) -> None:
    source = LIB_RS.read_text(encoding="utf-8")
    assert unstable_api not in source, (
        f"{unstable_api} needs the unstable `windows_by_handle` feature; "
        "use canonical paths or platform APIs available on stable instead"
    )


def test_crate_declares_a_stable_msrv() -> None:
    cargo = CARGO_TOML.read_text(encoding="utf-8")
    match = re.search(r'rust-version = "(\d+)\.(\d+)"', cargo)
    assert match, "Cargo.toml must declare rust-version"
    major, minor = int(match.group(1)), int(match.group(2))
    assert (major, minor) >= (1, 80), "pyo3 0.23 and the engine need Rust 1.80 or newer"


def test_pyo3_functions_expose_the_python_signature() -> None:
    source = LIB_RS.read_text(encoding="utf-8")
    # FileSystemScanner calls both functions with the traversal policy appended.
    expected = "signature = (path, token, follow_links=false, reparse_mounts=false)"
    assert source.count(expected) == 2, (
        "enumerate_directory and calculate_size must both accept optional "
        "follow_links/reparse_mounts arguments"
    )
    assert "py.allow_threads" in source, "filesystem work must release the GIL"


def test_engine_reports_skip_reasons_understood_by_python() -> None:
    """Every reason string the Rust engine emits must exist in SkipReason."""
    from disk_explorer.models.skip_reason import SkipReason

    source = LIB_RS.read_text(encoding="utf-8")
    emitted = set(re.findall(r'const SKIP_[A-Z_]+: &str = "([^"]+)"', source))
    assert emitted, "the Rust engine must report skip reasons"
    known = {reason.value for reason in SkipReason}
    assert emitted <= known, f"unknown skip reasons in Rust: {emitted - known}"
