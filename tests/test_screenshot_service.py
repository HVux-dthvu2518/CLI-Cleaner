from pathlib import Path
from types import ModuleType
import sys

import pytest

from disk_explorer.services.screenshot_service import save_screenshot


def test_svg_screenshot_writes_an_svg_file(tmp_path: Path) -> None:
    output = save_screenshot("<svg></svg>", tmp_path / "shot.png", "svg")
    assert output.name == "shot.svg"
    assert output.read_text(encoding="utf-8") == "<svg></svg>"


def test_png_screenshot_uses_rasterizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resvg = ModuleType("resvg_py")
    resvg.svg_to_bytes = lambda **_kwargs: b"fake-png"
    monkeypatch.setitem(sys.modules, "resvg_py", resvg)

    output = save_screenshot("<svg></svg>", tmp_path / "shot.svg", "png")

    assert output.name == "shot.png"
    assert output.read_bytes() == b"fake-png"


def test_jpg_alias_is_written_as_jpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resvg = ModuleType("resvg_py")
    resvg.svg_to_bytes = lambda **_kwargs: b"fake-png"
    monkeypatch.setitem(sys.modules, "resvg_py", resvg)

    class FakeImage:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def convert(self, _mode: str):
            return self

        def save(self, path: Path, format: str, quality: int) -> None:
            assert format == "JPEG"
            assert quality == 92
            Path(path).write_bytes(b"fake-jpeg")

    pil = ModuleType("PIL")
    pil.Image = ModuleType("PIL.Image")
    pil.Image.open = lambda _buffer: FakeImage()
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", pil.Image)

    output = save_screenshot("<svg></svg>", tmp_path / "shot.svg", "jpg")

    assert output.name == "shot.jpeg"
    assert output.read_bytes() == b"fake-jpeg"


def test_unsupported_format_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported screenshot format"):
        save_screenshot("<svg></svg>", tmp_path / "shot.gif", "gif")
