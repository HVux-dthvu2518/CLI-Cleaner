"""Launch the installed package or fall back to this checkout's Python sources."""

from importlib.util import find_spec
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
if find_spec("disk_explorer") is None:
    sys.path.insert(0, str(ROOT / "src"))

from disk_explorer.__main__ import main  # noqa: E402


if __name__ == "__main__":
    main()
