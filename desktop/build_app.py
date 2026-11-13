"""Build a native application on its target OS; never cross-claim a Mac build."""
from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
               "--name", "Vector", "--paths", str(ROOT), "--paths", str(ROOT.parent), "--distpath", str(args.output.resolve()),
               "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
               "--exclude-module", "PySide6.QtWebEngineCore", "--exclude-module", "PySide6.QtWebEngineWidgets"]
    command.extend(["--add-data", f"{ROOT / 'THIRD_PARTY.md'}:."])
    for name in ("PySide6-Essentials", "shiboken6"):
        distribution = importlib.metadata.distribution(name)
        for resource in distribution.files or []:
            if "licenses" in resource.parts:
                source = distribution.locate_file(resource)
                destination = Path("third-party") / name / resource.parent
                command.extend(["--add-data", f"{source}:{destination.as_posix()}"])
    if sys.platform == "darwin":
        command.extend(["--osx-bundle-identifier", "org.vector.research.desktop"])
    command.append(str(ROOT / "run.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    print(f"Built for {sys.platform}/{platform.machine()}; signing/notarization not configured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
