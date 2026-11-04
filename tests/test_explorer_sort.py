"""Explorer ordering: by name and by size, folders kept together.

Sorting is not cosmetic here: the point of "sort by size" is to find what is big, so
an unknown size must not pass for zero, and a folder whose size is still being
measured must not sit at the top pretending to be the largest.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable

from disk_explorer.application import DiskExplorerApp


def _tree(root: Path) -> Path:
    scope = root / "scope"
    (scope / "zeta").mkdir(parents=True)
    (scope / "alpha").mkdir()
    (scope / "zeta" / "big.bin").write_bytes(b"z" * 40_000)
    (scope / "alpha" / "small.txt").write_bytes(b"a" * 10)
    (scope / "middle.log").write_bytes(b"m" * 900)
    (scope / "a-file.txt").write_bytes(b"t" * 2_000)
    return scope


def _names(app: DiskExplorerApp) -> list[str]:
    return [entry.name for entry in app._entries]


@pytest.mark.asyncio
async def test_sort_by_name_ascending_and_descending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    scope = _tree(tmp_path)
    app = DiskExplorerApp()
    app.current_path = scope

    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause(0.6)
        app.sort_by("name", descending=False)
        await pilot.pause(0.2)
        assert _names(app) == ["alpha", "zeta", "a-file.txt", "middle.log"]

        app.sort_by("name", descending=True)
        await pilot.pause(0.2)
        # Folders stay on top even when reversing: they are a different kind of row.
        assert _names(app) == ["zeta", "alpha", "middle.log", "a-file.txt"]


@pytest.mark.asyncio
async def test_sort_by_size_puts_the_biggest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    scope = _tree(tmp_path)
    app = DiskExplorerApp()
    app.current_path = scope

    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause(0.8)
        app.sort_by("size", descending=True)
        await pilot.pause(0.6)

        assert _names(app)[0] == "zeta"  # 40 KB of files
        assert _names(app)[1] == "alpha"
        sizes = [entry.size for entry in app._entries if not entry.is_directory]
        assert sizes == sorted(sizes, reverse=True)
        assert sizes[0] == 2_000  # a-file.txt, not middle.log


@pytest.mark.asyncio
async def test_unknown_folder_size_never_ranks_as_zero_largest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    scope = _tree(tmp_path)
    (scope / "broken").mkdir()
    (scope / "broken" / "x.bin").write_bytes(b"x" * 5)
    app = DiskExplorerApp()
    app.current_path = scope

    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause(0.8)
        # Sorting happens while folder sizes are still arriving: those rows must not
        # be treated as 0 bytes and must not be reported as measured.
        app.sort_by("size", descending=True)
        await pilot.pause(0.5)

        first = app._entries[0]
        assert first.is_directory
        assert first.size is None or first.size > 0


@pytest.mark.asyncio
async def test_cycle_sort_and_reverse_stay_in_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    scope = _tree(tmp_path)
    app = DiskExplorerApp()
    app.current_path = scope

    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause(0.6)
        assert app.sort_key == "name"

        app.action_cycle_sort()
        await pilot.pause(0.2)
        assert app.sort_key == "size"

        app.action_reverse_sort()
        await pilot.pause(0.2)
        assert app.sort_descending is False

        app.action_cycle_sort()
        await pilot.pause(0.2)
        assert app.sort_key == "modified"


@pytest.mark.asyncio
async def test_sort_marker_is_visible_in_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    scope = _tree(tmp_path)
    app = DiskExplorerApp()
    app.current_path = scope

    async with app.run_test(size=(150, 44)) as pilot:
        await pilot.pause(0.6)
        app.sort_by("size", descending=True)
        await pilot.pause(0.2)
        table = app.query_one("#file-table", DataTable)
        assert "↓" in str(table.columns["size"].label)
        assert "↓" not in str(table.columns["name"].label)
