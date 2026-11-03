#!/usr/bin/env python3
"""Create a consistent compressed PostgreSQL snapshot and checksum manifest."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

from backup_lib import ROOT, checksum, compose, secure_create


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "backups" / "daily")
    parser.add_argument("--keep-days", type=int, default=14)
    parser.add_argument("--age-recipient", help="Optional age public recipient for encryption at rest")
    args = parser.parse_args()
    if args.keep_days < 1:
        parser.error("--keep-days must be positive")
    directory = args.output.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    now = datetime.now(timezone.utc)
    basename = f"trading-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    partial = directory / f"{basename}.dump.partial"
    final = directory / f"{basename}.dump"
    try:
        with secure_create(partial) as output:
            compose("exec", "-T", "database", "pg_dump", "-U", "postgres", "-d", "trading",
                    "--format=custom", "--compress=6", "--no-owner", "--no-acl", stdout=output)
            output.flush()
            os.fsync(output.fileno())
        if partial.stat().st_size == 0:
            raise RuntimeError("Database backup is empty")
        # pg_restore --list checks the archive header/TOC; restore_verify tests data.
        with partial.open("rb") as archive:
            compose("exec", "-T", "database", "pg_restore", "--list", stdin=archive,
                    stdout=subprocess.DEVNULL)
        if args.age_recipient:
            final = directory / f"{basename}.dump.age"
            with secure_create(final) as encrypted:
                subprocess.run(["age", "--recipient", args.age_recipient, str(partial)],
                               stdout=encrypted, check=True)
                encrypted.flush()
                os.fsync(encrypted.fileno())
            partial.unlink()
        else:
            partial.replace(final)
        manifest = {
            "schema_version": 1,
            "created_at": now.isoformat(),
            "file": final.name,
            "sha256": checksum(final),
            "size_bytes": final.stat().st_size,
            "format": "postgresql-custom",
            "encrypted": bool(args.age_recipient),
            "database": "trading",
            "includes_media": False,
            "restore_verified": False,
        }
        with secure_create(directory / f"{basename}.json") as output:
            output.write(json.dumps(manifest, indent=2).encode() + b"\n")
            output.flush()
            os.fsync(output.fileno())
        cutoff = time.time() - args.keep_days * 86400
        # Only known, complete backup pairs in this directory are eligible.
        for candidate in directory.glob("trading-*.json"):
            if candidate.is_symlink() or candidate.stat().st_mtime >= cutoff:
                continue
            old = json.loads(candidate.read_text())
            file_name = old.get("file", "")
            if Path(file_name).name != file_name or not file_name.startswith("trading-"):
                continue
            old_archive = directory / file_name
            if old_archive.is_file() and not old_archive.is_symlink():
                old_archive.unlink()
                candidate.unlink()
        print(f"Backup created: {final.name}; restore validation is still required.")
    finally:
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
