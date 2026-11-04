"""Export Textual's SVG screen capture in a configured image format."""

from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path

SUPPORTED_FORMATS = {"png", "svg", "jpeg"}


def _font_options() -> tuple[list[str], str]:
    """Find a local monospace font; resvg's font database may be empty."""
    if os.name == "nt":
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates = [fonts_dir / "consola.ttf", fonts_dir / "cour.ttf", fonts_dir / "arial.ttf"]
        family = "Consolas"
    else:
        fonts_dir = Path("/usr/share/fonts/truetype/dejavu")
        candidates = [fonts_dir / "DejaVuSansMono.ttf", fonts_dir / "DejaVuSans.ttf"]
        family = "DejaVu Sans Mono"
    return [str(font) for font in candidates if font.is_file()], family


def save_screenshot(svg: str, destination: Path, image_format: str) -> Path:
    """Write SVG directly or rasterize it to PNG/JPEG.

    PNG/JPEG conversion is optional because it adds image libraries to a
    terminal app. Install the ``screenshots`` extra to enable raster formats.
    """
    output_format = image_format.lower()
    if output_format == "jpg":
        output_format = "jpeg"
    if output_format not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported screenshot format: {image_format}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "svg":
        path = destination.with_suffix(".svg")
        path.write_text(svg, encoding="utf-8")
        return path

    try:
        from resvg_py import svg_to_bytes
    except ImportError as error:
        raise RuntimeError(
            "PNG/JPEG screenshot export needs optional dependencies. "
            "Install with: pip install 'disk-explorer-cleaner[screenshots]'"
        ) from error

    font_files, monospace_family = _font_options()
    png_bytes = svg_to_bytes(
        svg_string=svg,
        font_files=font_files or None,
        monospace_family=monospace_family,
    )
    if output_format == "png":
        path = destination.with_suffix(".png")
        path.write_bytes(png_bytes)
        return path

    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "JPEG screenshot export needs Pillow. "
            "Install with: pip install 'disk-explorer-cleaner[screenshots]'"
        ) from error
    path = destination.with_suffix(".jpeg")
    with Image.open(BytesIO(png_bytes)) as image:
        image.convert("RGB").save(path, format="JPEG", quality=92)
    return path
