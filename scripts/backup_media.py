#!/usr/bin/env python3
"""Back up private media after writers are stopped; database backup is separate."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets

from backup_lib import ROOT, checksum, compose, secure_create


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "backups" / "media")
    args = parser.parse_args()
    services = compose("ps", "--status", "running", "--services", capture_output=True, text=True)
    if {"backend", "worker", "scheduler"} & set(services.stdout.splitlines()):
        parser.error("Stop backend, worker and scheduler before a consistent media snapshot")
    directory = args.output.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    created = datetime.now(timezone.utc)
    name = f"media-{created:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    partial = directory / f"{name}.tar.gz.partial"
    final = directory / f"{name}.tar.gz"
    try:
        with secure_create(partial) as output:
            compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "tar", "backend",
                    "-C", "/app/media", "-czf", "-", ".", stdout=output)
            output.flush()
            os.fsync(output.fileno())
        partial.replace(final)
        manifest = {"schema_version": 1, "created_at": created.isoformat(), "file": final.name,
                    "sha256": checksum(final), "size_bytes": final.stat().st_size,
                    "format": "media-tar-gzip", "writers_stopped": True,
                    "restore_verified": False}
        with secure_create(directory / f"{name}.json") as output:
            output.write(json.dumps(manifest, indent=2).encode() + b"\n")
        print(f"Media backup created: {final.name}. Encrypt before off-host transfer.")
    finally:
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
