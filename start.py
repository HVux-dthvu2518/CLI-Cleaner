"""Launch the application from a source checkout, independent of the working directory."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from disk_explorer.__main__ import main  # noqa: E402


if __name__ == "__main__":
    main()
