#!/usr/bin/env python3
"""Package reviewed source only; never copy local runtime data or credentials."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {".github", "backend", "engine", "execution", "ml", "infrastructure", "scripts", "tests", "docs"}
FILES = {".dockerignore", ".gitignore", ".env.example", "Dockerfile", "docker-compose.yml",
         "pyproject.toml", "requirements.txt", "requirements-dev.txt", "requirements-ml.txt", "VERSION"}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".cache", "media", "emails", "backups"}


def included(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if not (rel.parts[0] in DIRECTORIES or len(rel.parts) == 1 and (path.name in FILES or path.suffix == ".md")):
        return False
    if EXCLUDED_PARTS.intersection(rel.parts):
        return False
    if path.name.startswith(".env") and not path.name.endswith(".example"):
        return False
    return path.suffix not in {".pyc", ".pyo", ".sqlite3", ".pem", ".key", ".dump"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    version = (ROOT / "VERSION").read_text().strip()
    parser.add_argument("--output", type=Path, default=ROOT.parent / f"trading-system-v{version}.tar.gz")
    args = parser.parse_args()
    selected = sorted(p for p in ROOT.rglob("*") if p.is_file() and included(p))
    manifest = {"version": version, "scope": "source and selected non-secret evidence; no deployment credentials",
                "files": []}
    with tarfile.open(args.output, "w:gz") as archive:
        for path in selected:
            if path.is_symlink():
                raise RuntimeError("Source symlinks require explicit review")
            data = path.read_bytes()
            name = path.relative_to(ROOT).as_posix()
            entry = tarfile.TarInfo("trading-system/" + name)
            entry.size = len(data)
            entry.mode = 0o755 if path.suffix == ".sh" else 0o644
            archive.addfile(entry, io.BytesIO(data))
            manifest["files"].append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        data = json.dumps(manifest, indent=2).encode() + b"\n"
        entry = tarfile.TarInfo("trading-system/SOURCE_MANIFEST.json")
        entry.size = len(data)
        entry.mode = 0o644
        archive.addfile(entry, io.BytesIO(data))
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(f"{digest}  {args.output.name}\n")
    print(f"Packaged {len(selected)} source files: {args.output.name} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
