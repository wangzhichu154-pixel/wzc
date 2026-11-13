"""Source and PyInstaller entry point."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vector_desktop.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
