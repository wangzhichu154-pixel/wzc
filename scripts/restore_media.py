#!/usr/bin/env python3
"""Validate and restore media into a NEW volume; never overwrite live uploads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import secrets
import subprocess
import tarfile

from backup_lib import checksum, compose

RESTORE_CODE = r'''
import os, pathlib, sys, tarfile
root = pathlib.Path('/app/media')
if any(root.iterdir()):
    raise SystemExit('Refusing nonempty target volume')
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
    for item in archive:
        path = pathlib.PurePosixPath(item.name)
        if path.is_absolute() or '..' in path.parts or not (item.isfile() or item.isdir()):
            raise SystemExit('Unsafe archive member')
        target = root.joinpath(*path.parts)
        if item.isdir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target, 0o700)
        else:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(item)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                while chunk := source.read(1048576):
                    out.write(chunk)
print('Private media restored into an isolated volume')
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=10 * 1024**3)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    name = manifest["file"]
    if Path(name).name != name or manifest.get("format") != "media-tar-gzip":
        parser.error("Invalid media manifest")
    archive_path = manifest_path.parent / name
    if archive_path.is_symlink() or not archive_path.is_file():
        parser.error("Media archive must be a regular file")
    if checksum(archive_path) != manifest["sha256"] or archive_path.stat().st_size != manifest["size_bytes"]:
        parser.error("Media checksum/size mismatch")
    total = 0
    members = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                parser.error("Unsafe archive member")
            total += member.size
            members += 1
            if total > args.max_bytes or members > 1_000_000:
                parser.error("Archive exceeds configured extraction limits")
    volume = "restored_media_" + secrets.token_hex(8)
    subprocess.run(["docker", "volume", "create", volume], check=True, stdout=subprocess.DEVNULL)
    success = False
    try:
        with archive_path.open("rb") as source:
            compose("run", "--rm", "--no-deps", "-T", "--volume", f"{volume}:/app/media",
                    "--entrypoint", "python", "backend", "-c", RESTORE_CODE, stdin=source)
        success = True
        print(f"Media restore passed: {members} entries, {total} bytes; volume={volume}; kept={args.keep}")
    finally:
        if not (success and args.keep):
            subprocess.run(["docker", "volume", "rm", volume], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
