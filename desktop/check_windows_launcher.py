"""Test the exact source-launcher batch file in a spaced Unicode directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("This check must run on Windows")
    with tempfile.TemporaryDirectory(prefix="Vector Source 中文 ") as temporary:
        source = Path(temporary) / "desktop"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
            ".venv", "build", "dist", "release", "__pycache__", "*.pyc"))
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", CI="true", PYTHONUTF8="1")
        command = [os.environ["COMSPEC"], "/d", "/c", "call",
                   str(source / "Start-Vector.cmd"), "--smoke-test"]
        subprocess.run(command, cwd=source, env=environment, check=True, timeout=240)
        if not (source / ".venv/Scripts/python.exe").is_file():
            raise RuntimeError("Launcher did not create an isolated Windows environment")
        if (source / ".env").exists() or (source / ".secrets").exists():
            raise RuntimeError("Desktop launcher unexpectedly initialized backend credentials")
    report = {"status": "passed", "source_launcher_executed": True,
              "unicode_and_space_path": True, "backend_credentials_created": False}
    (ROOT / "release").mkdir(exist_ok=True)
    (ROOT / "release/windows-launcher-verification.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
