"""Verified capture manifests and forward labels kept outside causal features."""
from __future__ import annotations

from bisect import bisect_left
import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from .features import FeatureConfig, FeatureState
from .model import canonical_bytes, _unique_pairs

CSV_FIELDS = ("ordinal", "timestamp_ns", "logical_timestamp_ns", "stock_locate", "bid", "ask",
              "bid_qty", "ask_qty", "valid", "tradable")
DAY_NS = 86_400_000_000_000
MAX_CAPTURE_BYTES = 512 * 1024 * 1024
MAX_OBSERVATIONS = 2_000_000


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Capture:
    capture_id: str
    session_date: str
    symbol: str
    stock_locate: int
    itch_path: Path
    itch_sha256: str
    market_csv_path: Path
    market_csv_sha256: str


@dataclass(frozen=True)
class DatasetManifest:
    path: Path
    captures: tuple[Capture, ...]
    provenance: dict
    dataset_sha256: str
    manifest_sha256: str
    raw: dict


@dataclass(frozen=True)
class LabeledSample:
    capture_id: str
    session_date: str
    sequence: int
    timestamp_ns: int
    label_end_ns: int
    segment: int
    mid_units: float
    features: tuple[float, ...]
    target_bps: float

    @property
    def absolute_timestamp_ns(self):
        # A chronology key only: ITCH time-of-day is not claimed to be UTC.
        return date.fromisoformat(self.session_date).toordinal() * DAY_NS + self.timestamp_ns

    @property
    def absolute_label_end_ns(self):
        return date.fromisoformat(self.session_date).toordinal() * DAY_NS + self.label_end_ns


def _verified_path(root, value, expected_hash):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError("Capture paths must be relative to the manifest directory")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("A SHA256 digest is required for each capture and market CSV")
    path = root / value
    if ".." in Path(value).parts or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise ValueError("Capture path traversal and symbolic links are not allowed")
    if not path.resolve().is_relative_to(root) or not path.is_file() or not 0 < path.stat().st_size <= MAX_CAPTURE_BYTES:
        raise ValueError("Capture must be a nonempty bounded regular file inside the dataset")
    if sha256_file(path) != expected_hash:
        raise ValueError("Capture content does not match its manifest SHA256")
    return path.resolve()


def load_manifest(path) -> DatasetManifest:
    """Validate schema, licensing attestation, ordering identities and both hashes.

    Attestation is an operator declaration, not independently verified entitlement.
    This function never upgrades synthetic provenance to real market data.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Manifest must be a bounded regular JSON file")
    raw = json.loads(path.read_text(), object_pairs_hook=_unique_pairs,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "provenance", "captures"} or type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("Unsupported dataset manifest schema")
    provenance = raw["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"kind", "source", "authorized_use_attested"}:
        raise ValueError("Dataset provenance and authorization attestation are required")
    if provenance["kind"] not in ("synthetic", "licensed_market") or not isinstance(provenance["source"], str) or not 1 <= len(provenance["source"]) <= 512 or type(provenance["authorized_use_attested"]) is not bool:
        raise ValueError("Invalid provenance declaration")
    if provenance["kind"] == "licensed_market" and not provenance["authorized_use_attested"]:
        raise ValueError("Licensed market data requires an operator use-rights attestation")
    if not isinstance(raw["captures"], list) or not 1 <= len(raw["captures"]) <= 2500:
        raise ValueError("A bounded list of session captures is required")
    root = path.resolve().parent
    captures, seen_ids, seen_sessions, seen_itch, seen_csv = [], set(), set(), set(), set()
    required = {"capture_id", "session_date", "symbol", "stock_locate", "itch_path", "itch_sha256", "market_csv_path", "market_csv_sha256"}
    for entry in raw["captures"]:
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("Unsupported capture entry")
        ident, symbol = entry["capture_id"], entry["symbol"]
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", ident) or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", symbol):
            raise ValueError("Invalid capture identity or symbol")
        if type(entry["stock_locate"]) is not int or not 1 <= entry["stock_locate"] <= 65535:
            raise ValueError("Invalid ITCH stock locate")
        try:
            day = date.fromisoformat(entry["session_date"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid declared exchange session date") from exc
        if day.isoformat() != entry["session_date"]:
            raise ValueError("Session date must use ISO calendar format")
        session_key = (symbol, day.isoformat())
        if ident in seen_ids or session_key in seen_sessions or entry["itch_sha256"] in seen_itch or entry["market_csv_sha256"] in seen_csv:
            raise ValueError("Duplicate capture, session, source hash, or market CSV hash")
        itch = _verified_path(root, entry["itch_path"], entry["itch_sha256"])
        market = _verified_path(root, entry["market_csv_path"], entry["market_csv_sha256"])
        captures.append(Capture(ident, day.isoformat(), symbol, entry["stock_locate"], itch,
                                entry["itch_sha256"], market, entry["market_csv_sha256"]))
        seen_ids.add(ident)
        seen_sessions.add(session_key)
        seen_itch.add(entry["itch_sha256"])
        seen_csv.add(entry["market_csv_sha256"])
    if len({capture.symbol for capture in captures}) != 1:
        raise ValueError("This model version requires one symbol per dataset")
    captures.sort(key=lambda capture: (capture.session_date, capture.capture_id))
    identity = {"provenance": provenance, "captures": [
        {key: value for key, value in entry.items() if key not in ("itch_path", "market_csv_path")}
        for entry in sorted(raw["captures"], key=lambda item: (item["session_date"], item["capture_id"]))]}
    return DatasetManifest(path.resolve(), tuple(captures), provenance,
                           hashlib.sha256(canonical_bytes(identity)).hexdigest(), sha256_file(path), raw)


def iter_market_rows(capture: Capture):
    """Yield checked exporter rows, mapping file ordinal to feature sequence."""
    previous_sequence, previous_time = 0, -1
    with capture.market_csv_path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise ValueError("Market CSV does not match the C++ exporter schema")
        for count, raw in enumerate(reader, 1):
            if count > MAX_OBSERVATIONS or None in raw or any(value is None or not re.fullmatch(r"[0-9]{1,20}", value) for value in raw.values()):
                raise ValueError("Market CSV exceeds bounds or contains a noninteger field")
            row = {name: int(value) for name, value in raw.items()}
            sequence, now = row["ordinal"], row["timestamp_ns"]
            if not previous_sequence < sequence < 2**63 or not previous_time <= now < DAY_NS:
                raise ValueError("Market observations must preserve strict ordinal and causal time order")
            if row["logical_timestamp_ns"] != now or row["stock_locate"] != capture.stock_locate:
                raise ValueError("Market CSV contains a repaired/regressed clock or the wrong stock locate")
            if row["valid"] not in (0, 1) or row["tradable"] not in (0, 1) or row["tradable"] > row["valid"]:
                raise ValueError("Invalid market status flags")
            previous_sequence, previous_time = sequence, now
            row["sequence"] = row.pop("ordinal")
            yield row
        if previous_sequence == 0:
            raise ValueError("Capture contains no market observations")


def verify_market_exports(dataset: DatasetManifest, exporter=None):
    """Rebuild derived CSV from original ITCH; two unrelated hashes are insufficient."""
    executable = Path(exporter) if exporter else Path(__file__).resolve().parents[1] / "build" / "trading-ml-replay"
    if executable.is_symlink() or not executable.is_file() or not os_access_executable(executable):
        raise ValueError("Build trading-ml-replay before training to verify source/feature lineage")
    executable = executable.resolve()
    exporter_sha256 = sha256_file(executable)
    with tempfile.TemporaryDirectory(prefix="verified-itch-export-") as directory:
        for index, capture in enumerate(dataset.captures):
            destination = Path(directory) / f"capture-{index}.csv"
            completed = subprocess.run([str(executable), "--input", str(capture.itch_path),
                                        "--symbol-locate", str(capture.stock_locate), "--export-market", str(destination)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120, check=False)
            if completed.returncode or not destination.is_file():
                raise ValueError(f"C++ source verification failed for capture {capture.capture_id}")
            if sha256_file(destination) != capture.market_csv_sha256:
                raise ValueError(f"Market CSV does not match the original ITCH export: {capture.capture_id}")
    if sha256_file(executable) != exporter_sha256:
        raise ValueError("Exporter binary changed during verification")
    return {"market_exports_verified": True, "exporter_sha256": exporter_sha256}


def os_access_executable(path):
    import os
    return os.access(path, os.X_OK)


def label_capture(capture: Capture, feature_config: FeatureConfig, horizon_ns: int,
                  max_label_delay_ns: int) -> list[LabeledSample]:
    """Forward labels may use future midpoint; features never do.

    Future observations must be within the same uninterrupted valid segment.
    Only emitted first-timestamp observations can become feature/label anchors;
    no use of a timestamp's hindsight final quote or forward-filled invalid book.
    """
    state, observations = FeatureState(feature_config), []
    for row in iter_market_rows(capture):
        sample = state.update(row)
        if sample is not None:
            observations.append((sample, state.resets))
    timestamps = [sample.timestamp_ns for sample, _ in observations]
    labeled = []
    for index, (sample, segment) in enumerate(observations):
        target_time = sample.timestamp_ns + horizon_ns
        future_index = bisect_left(timestamps, target_time, lo=index + 1)
        if future_index >= len(observations):
            continue
        future, future_segment = observations[future_index]
        if future_segment != segment or future.timestamp_ns > target_time + max_label_delay_ns:
            continue
        target = 10_000.0 * (future.mid_units / sample.mid_units - 1.0)
        if abs(target) > 10_000:
            raise ValueError("Unbounded future price move: inspect price adjustment and source integrity")
        labeled.append(LabeledSample(capture.capture_id, capture.session_date, sample.sequence,
                                     sample.timestamp_ns, future.timestamp_ns, segment,
                                     sample.mid_units, sample.features, target))
    return labeled


def build_labeled_sessions(dataset: DatasetManifest, feature_config: FeatureConfig,
                           horizon_ns: int, max_label_delay_ns: int):
    return [(capture, label_capture(capture, feature_config, horizon_ns, max_label_delay_ns))
            for capture in dataset.captures]
