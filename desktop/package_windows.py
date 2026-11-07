"""Produce and execute the Windows x64 portable distribution on Windows itself."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
PACKAGE = "Vector-Windows-x64"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def pe_machine(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("Executable is not a Windows PE image")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise ValueError("Invalid PE header offset")
        stream.seek(offset)
        signature = stream.read(6)
        if len(signature) != 6 or signature[:4] != b"PE\0\0":
            raise ValueError("Invalid PE signature")
        return struct.unpack_from("<H", signature, 4)[0]


def create_archive(source: Path, output: Path, readme: Path) -> dict[str, str]:
    if not (source / "Vector.exe").is_file() or not (source / "_internal").is_dir():
        raise ValueError("The complete PyInstaller application directory is required")
    if pe_machine(source / "Vector.exe") != 0x8664:
        raise ValueError("Expected an x64 Windows executable")
    source = source.resolve()
    if output.resolve().is_relative_to(source):
        raise ValueError("Archive output must be outside the application directory")
    files = sorted(source.rglob("*"))
    if any(path.is_symlink() for path in files):
        raise ValueError("Unexpected application symlink")
    manifest = {}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files:
            if path.is_file():
                name = PACKAGE + "/" + path.relative_to(source).as_posix()
                bundle.write(path, name)
                manifest[name] = sha256(path)
        name = PACKAGE + "/READ-ME.txt"
        content = readme.read_bytes()
        bundle.writestr(name, content)
        manifest[name] = hashlib.sha256(content).hexdigest()
        bundle.writestr(PACKAGE + "/MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist/Vector")
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows packaging and execution require a real Windows runner")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / (PACKAGE + ".zip")
    manifest = create_archive(args.dist, archive, ROOT / "WINDOWS.md")
    if not any(Path(name).name.lower() == "qwindows.dll" for name in manifest):
        raise ValueError("Windows native Qt platform plugin is missing")
    with tempfile.TemporaryDirectory(prefix="Vector Windows 中文 ") as temporary:
        extracted = Path(temporary)
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise ValueError("Distribution CRC failed")
            bundle.extractall(extracted)
        for name, digest in manifest.items():
            if sha256(extracted / name) != digest:
                raise ValueError("Extracted distribution checksum mismatch")
        executable = extracted / PACKAGE / "Vector.exe"
        # Wait for the GUI process explicitly. A bare PowerShell call may return
        # before a windowed executable has actually initialized or failed.
        for qt_platform in ("offscreen", "windows"):
            environment = dict(os.environ, QT_QPA_PLATFORM=qt_platform)
            subprocess.run([str(executable), "--smoke-test"], cwd=executable.parent,
                           env=environment, check=True, timeout=60)
    digest = sha256(archive)
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    report = {"status": "passed", "platform": platform.platform(), "architecture": platform.machine(),
              "source_commit": os.environ.get("GITHUB_SHA", ""), "pe_machine": "0x8664",
              "files_verified": len(manifest), "archive_sha256": digest,
              "extracted_unicode_and_space_path_launch": True,
              "qt_platforms_tested": ["offscreen", "windows"], "native_platform_plugin_included": True,
              "backend_started": False, "user_windows_device_tested": False,
              "code_signing_configured": False, "live_trading": False}
    (args.output / "windows-verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
