"""
Primary repository launcher for the Cascade Research Tool.

Run this file from the repository root:

    uv run python Script/run_application.py
"""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIRECTORY.parent
SOURCE_ROOT = REPOSITORY_ROOT / "src"

# The repository currently uses src/ layout with package = false. Add the
# package root explicitly until the project is converted into an installable
# package with a console-script entry point.
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cascade_research_tool.app import main


if __name__ == "__main__":
    main()