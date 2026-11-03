"""Export checked ITCH inputs through the actual C++ book builder."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from .data import _verified_path, load_manifest, sha256_file
from .model import _unique_pairs, write_json


def prepare(source_manifest, binary):
    source = Path(source_manifest)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Source manifest must be a bounded regular file")
    root = source.resolve().parent
    destination = root / "manifest.json"
    if destination.exists():
        raise ValueError("Prepared dataset exists; choose a new immutable dataset directory")
    payload = json.loads(source.read_text(), object_pairs_hook=_unique_pairs)
    if set(payload) != {"schema_version", "provenance", "captures"} or payload["schema_version"] != 1:
        raise ValueError("Unsupported source manifest")
    if not isinstance(payload["captures"], list) or not 1 <= len(payload["captures"]) <= 2500:
        raise ValueError("Invalid session count")
    binary = Path(binary).resolve(strict=True)
    output = root / "market"
    output.mkdir(mode=0o700, exist_ok=False)
    captures = []
    for index, entry in enumerate(payload["captures"]):
        if not isinstance(entry, dict) or set(entry) != {"capture_id", "session_date", "symbol", "stock_locate", "itch_path", "itch_sha256"}:
            raise ValueError("Unsupported source capture schema")
        if type(entry["stock_locate"]) is not int or not 1 <= entry["stock_locate"] <= 65535:
            raise ValueError("Invalid stock locate")
        itch = _verified_path(root, entry["itch_path"], entry["itch_sha256"])
        csv_path = output / f"session-{index:04d}.csv"
        result = subprocess.run([str(binary), "--input", str(itch), "--symbol-locate", str(entry["stock_locate"]),
                                 "--export-market", str(csv_path)], capture_output=True, timeout=120, check=False)
        if result.returncode != 0:
            raise ValueError(f"C++ export rejected capture {index}: {result.stderr[-1000:].decode(errors='replace')}")
        if sha256_file(itch) != entry["itch_sha256"]:
            raise ValueError("Source changed during export")
        captures.append({**entry, "market_csv_path": csv_path.relative_to(root).as_posix(),
                         "market_csv_sha256": sha256_file(csv_path)})
    write_json(destination, {"schema_version": 1, "provenance": payload["provenance"], "captures": captures})
    dataset = load_manifest(destination)
    write_json(root / "export-evidence.json", {"schema_version": 1, "engine_sha256": sha256_file(binary),
               "source_manifest_sha256": sha256_file(source), "dataset_sha256": dataset.dataset_sha256,
               "sessions": len(captures), "parser": "C++ ITCH order book; no Python price fabrication"})
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--binary", type=Path, default=Path("build/trading-ml-replay"))
    args = parser.parse_args()
    try:
        print(prepare(args.source_manifest, args.binary))
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"Preparation failed: {exc}\n")


if __name__ == "__main__":
    main()
