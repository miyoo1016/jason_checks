"""CHECKS Terminal launcher — run from project root."""

import sys
from pathlib import Path

# Add src/ to Python path so `jason_checks` package is importable
ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT / "src"))

from jason_checks.tui_main import main

if __name__ == "__main__":
    main()
