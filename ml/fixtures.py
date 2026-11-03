"""Deterministic synthetic ITCH captures for software tests, never market evidence.

This generator emits protocol messages, not invented training labels or PnL.
Prices follow a seeded stochastic process. Only the real C++ book exporter may
turn these files into feature inputs. Session lifecycle markers are complete;
observations cover a short intraday interval, not a captured historical day.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import math
from pathlib import Path
import random
import struct

from .data import sha256_file
from .features import REGULAR_CLOSE_NS, REGULAR_OPEN_NS
from .model import write_json


def _header(kind, timestamp_ns, locate=1):
    return kind.encode("ascii") + struct.pack(">HH", locate, 0) + timestamp_ns.to_bytes(6, "big")


def _directory(timestamp_ns):
    raw = bytearray(39)
    raw[:11] = _header("R", timestamp_ns)
    raw[11:19] = b"TEST    "
    raw[19:21] = b"QN"
    raw[21:25] = (100).to_bytes(4, "big")
    raw[25:29] = b"NCNN"
    raw[29:31] = b"  "
    raw[31:35] = b" NNN"
    raw[35:39] = b"\0\0\0N"
    return bytes(raw)


def session_frames(seed, session_index, steps):
    rng = random.Random(seed + session_index * 104729)
    start = REGULAR_OPEN_NS
    yield _header("S", start - 3_000_000, 0) + b"O"
    yield _directory(start - 2_000_000)
    yield _header("H", start - 1_000_000) + b"TEST    T     "
    yield _header("S", start, 0) + b"Q"
    # Session-index variation ensures truly distinct protocol input hashes.
    mid_ticks = 10_000 + session_index * 11
    spread_ticks = 4
    next_reference, match = 3, 1
    references, quantities = [1, 2], [140, 140]
    for side, reference, price in ((b"B", 1, (mid_ticks - 2) * 100), (b"S", 2, (mid_ticks + 2) * 100)):
        yield _header("A", start + 1_000_000) + struct.pack(">QcI8sI", reference, side, 140, b"TEST    ", price)
    pressure = 0.0
    for index in range(steps):
        now = start + (index + 1) * 100_000_000
        # No future state is injected into observations. A stochastic latent
        # state drives both present depth and later noisy increments.
        volatility = (0.35, 0.65, 1.1, 0.5)[session_index % 4]
        pressure = 0.86 * pressure + rng.gauss(0.0, 0.45)
        move = rng.gauss(0.12 * pressure, volatility)
        mid_ticks += max(-1, min(1, int(round(move))))
        spread_ticks = 4 + 2 * int(abs(pressure) > 1.0) + 2 * int(session_index % 4 == 2 and index % 70 < 10)
        prices = [(mid_ticks - spread_ticks // 2) * 100, (mid_ticks + spread_ticks // 2) * 100]
        for side_index, side in enumerate((b"B", b"S")):
            depth = max(30, min(500, int(140 + (1 if side_index == 0 else -1) * 55 * math.tanh(pressure) + rng.gauss(0, 15))))
            replacement = next_reference
            next_reference += 1
            yield _header("U", now + side_index * 1_000_000) + struct.pack(">QQII", references[side_index], replacement, depth, prices[side_index])
            references[side_index], quantities[side_index] = replacement, depth
        # Add behind existing visible queue, consume the ahead order, then part
        # of the later order. This lets the real FIFO simulator decide whether
        # a resting strategy order was eligible, without treating a touch as fill.
        for side_index, side in enumerate((b"B", b"S")):
            base = now + 10_000_000 + side_index * 30_000_000
            later = next_reference
            next_reference += 1
            quantity = rng.randint(35, 65)
            yield _header("A", base) + struct.pack(">QcI8sI", later, side, quantity, b"TEST    ", prices[side_index])
            yield _header("E", base + 10_000_000) + struct.pack(">QIQ", references[side_index], quantities[side_index], match)
            match += 1
            filled = rng.randint(1, quantity // 2)
            yield _header("E", base + 20_000_000) + struct.pack(">QIQ", later, filled, match)
            match += 1
            references[side_index], quantities[side_index] = later, quantity - filled
    yield _header("S", REGULAR_CLOSE_NS, 0) + b"M"
    yield _header("S", REGULAR_CLOSE_NS + 1_000_000, 0) + b"C"


def generate_dataset(output, sessions=20, steps=2400, seed=42, start_date="2026-06-01"):
    if type(sessions) is not int or not 10 <= sessions <= 250 or type(steps) is not int or not 128 <= steps <= 100_000 or type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("Fixture requires 10..250 sessions, 128..100000 steps and an unsigned seed")
    try:
        day = date.fromisoformat(start_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("Fixture start date must be an ISO calendar date") from exc
    if day.isoformat() != start_date:
        raise ValueError("Fixture start date must use YYYY-MM-DD")
    output = Path(output)
    if output.exists():
        raise ValueError("Synthetic fixture output must be a new directory")
    (output / "captures").mkdir(parents=True, mode=0o700)
    manifest = {"schema_version": 1, "provenance": {
        "kind": "synthetic", "source": f"deterministic synthetic ITCH fixture seed={seed}; short intraday windows; no market-performance evidence",
        "authorized_use_attested": False}, "captures": []}
    for index in range(sessions):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        identifier = "synthetic-" + day.isoformat()
        relative = "captures/" + identifier + ".itch"
        destination = output / relative
        with destination.open("xb") as stream:
            for frame in session_frames(seed, index, steps):
                stream.write(len(frame).to_bytes(2, "big"))
                stream.write(frame)
        manifest["captures"].append({"capture_id": identifier, "session_date": day.isoformat(), "symbol": "TEST", "stock_locate": 1,
                                     "itch_path": relative, "itch_sha256": sha256_file(destination)})
        day += timedelta(days=1)
    write_json(output / "source-manifest.json", manifest)
    write_json(output / "fixture-config.json", {"seed": seed, "sessions": sessions, "steps": steps, "start_date": start_date,
                                                "kind": "synthetic", "purpose": "protocol and training pipeline verification"})
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--steps", type=int, default=2400)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start-date", default="2026-06-01", help="Declared synthetic ISO session start date; not market-data provenance")
    args = parser.parse_args(argv)
    try:
        manifest = generate_dataset(args.output, args.sessions, args.steps, args.seed, args.start_date)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"kind": "synthetic", "sessions": len(manifest["captures"]), "seed": args.seed,
                      "source_manifest": str(Path(args.output) / "source-manifest.json"), "market_validation": False}))


if __name__ == "__main__":
    raise SystemExit(main())
