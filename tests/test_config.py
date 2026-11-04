import json
from pathlib import Path

from disk_explorer.config import AppConfig, load_config, save_config


def test_config_round_trip_and_worker_bounds(tmp_path: Path) -> None:
    path = tmp_path / "data" / "settings.json"
    saved = AppConfig(max_workers=99, show_details=False)
    save_config(saved, path)

    loaded = load_config(path)
    assert loaded.show_details is False
    assert loaded.max_workers == 32
    assert json.loads(path.read_text(encoding="utf-8"))["max_workers"] == 99


def test_invalid_config_uses_defaults(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert load_config(path) == AppConfig()


def test_invalid_worker_value_falls_back_to_default(tmp_path: Path) -> None:
    path = tmp_path / "wrong-types.json"
    path.write_text('{"max_workers": "many", "cache_ttl_minutes": null, "screenshot_format": "bmp"}', encoding="utf-8")
    loaded = load_config(path)
    assert loaded.max_workers == AppConfig().max_workers
    assert loaded.cache_ttl_minutes == AppConfig().cache_ttl_minutes
    assert loaded.screenshot_format == "png"


def test_jpg_screenshot_setting_normalizes_to_jpeg(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"screenshot_format": "JPG"}', encoding="utf-8")
    assert load_config(path).screenshot_format == "jpeg"
