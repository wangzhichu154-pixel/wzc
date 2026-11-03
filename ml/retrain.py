"""One bounded scheduler tick: detect drift, train new data, register a candidate.

Never promotes, changes active pointers, launches a broker or relaxes a gate.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .data import load_manifest
from .features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION
from .model import LinearModel, canonical_bytes
from .registry import Registry, RegistryError, _decode, _mkdir, _present, _write, read_bytes, read_json
from .reservations import ReservationConflict, check_available, reserve
from .training import TrainingConfig

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RetrainConfig:
    interval_seconds: int = 86400
    min_samples: int = 200
    mean_z_threshold: float = 2.0
    psi_threshold: float = 0.2
    timeout_seconds: int = 1800
    max_window_age_seconds: int = 3600

    def validate(self):
        for name in ("interval_seconds", "min_samples", "timeout_seconds", "max_window_age_seconds"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 31_536_000:
                raise RegistryError("Invalid retraining bound")
        if self.min_samples > 10000 or self.timeout_seconds > 7200:
            raise RegistryError("Retraining workload exceeds supported bounds")
        for value in (self.mean_z_threshold, self.psi_threshold):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 100:
                raise RegistryError("Invalid drift threshold")
        return self


def detect_drift(model: LinearModel, samples, config=None):
    config = (config or RetrainConfig()).validate()
    if not isinstance(samples, list) or len(samples) > 10000:
        raise RegistryError("Feature window must be a bounded list")
    for row in samples:
        if not isinstance(row, (list, tuple)) or len(row) != len(FEATURE_NAMES):
            raise RegistryError("Feature window dimension mismatch")
        if any(type(x) not in (int, float) or not math.isfinite(x) or abs(x) > 1e12 for x in row):
            raise RegistryError("Invalid feature observation")
    if len(samples) < config.min_samples:
        return {"evaluated": False, "drift_detected": None, "reason": "INSUFFICIENT_WINDOW",
                "samples": len(samples), "features": {}}
    reference = model.training_metadata.get("feature_reference", {})
    if reference.get("fit_scope") != "training_only":
        raise RegistryError("Drift reference must be fitted on training data only")
    values, triggered = {}, False
    for index, name in enumerate(FEATURE_NAMES):
        column = [row[index] for row in samples]
        z = abs(math.fsum(column) / len(column) - model.mean[index]) / model.scale[index]
        base = reference.get("features", {}).get(name, {})
        edges, counts = base.get("edges"), base.get("counts")
        psi = None
        if edges is not None or counts is not None:
            if (not isinstance(edges, list) or not isinstance(counts, list) or not 2 <= len(edges) <= 101
                    or len(counts) != len(edges) - 1
                    or any(type(x) not in (int, float) or not math.isfinite(x) for x in edges)
                    or any(a >= b for a, b in zip(edges, edges[1:]))
                    or any(type(x) is not int or x < 0 for x in counts) or sum(counts) == 0):
                raise RegistryError("Invalid training PSI reference")
            # Keep explicit underflow/overflow buckets. Clamping into edge bins
            # hides out-of-support drift, especially for a constant feature.
            expected = [0, *counts, 0]
            observed = [0] * len(expected)
            for value in column:
                bucket = (0 if value < edges[0] else len(expected) - 1 if value > edges[-1]
                          else 1 + min(len(counts) - 1, bisect_right(edges, value) - 1))
                observed[bucket] += 1
            smooth = 0.5
            expected_total, actual_total = sum(expected) + smooth * len(expected), len(column) + smooth * len(expected)
            psi = math.fsum(((a + smooth) / actual_total - (e + smooth) / expected_total)
                            * math.log(((a + smooth) / actual_total) / ((e + smooth) / expected_total))
                            for a, e in zip(observed, expected))
        detected = z >= config.mean_z_threshold or (psi is not None and psi >= config.psi_threshold)
        triggered |= detected
        values[name] = {"standardized_mean_shift": z, "psi": psi, "drift_detected": detected}
    return {"evaluated": True, "drift_detected": triggered, "reason": "FEATURE_DRIFT" if triggered else "STABLE",
            "samples": len(samples), "features": values}


def _window(path, model, config, now):
    value = read_json(path)
    if (value.get("schema_version") != 1 or value.get("model_version") != model.model_version
            or value.get("feature_schema_version") != FEATURE_SCHEMA_VERSION or value.get("feature_names") != list(FEATURE_NAMES)):
        raise RegistryError("Drift window model/feature schema mismatch")
    observed = value.get("observed_at_ns")
    if type(observed) is not int or observed > now + 1_000_000_000 or now - observed > config.max_window_age_seconds * 1_000_000_000:
        raise RegistryError("Drift window is stale or future-dated")
    return detect_drift(model, value.get("samples"), config)


def _latest(registry, state, reason, *, now, extra=None):
    record = {"schema_version": 1, "state": state, "reason": reason, "updated_at_ns": now, **(extra or {})}
    _write(registry.root / "retrain" / "latest.json", record, replace=True)
    return record


def _frozen_training_arguments(model):
    configuration = model.training_metadata.get("config")
    if not isinstance(configuration, dict) or not isinstance(configuration.get("training"), dict):
        raise RegistryError("Complete frozen training configuration is required")
    training = configuration["training"]
    if set(training) != set(asdict(TrainingConfig())) or not isinstance(training.get("alphas"), list):
        raise RegistryError("Unsupported frozen training configuration")
    try:
        checked = TrainingConfig(**{**training, "alphas": tuple(training["alphas"])}).validate()
    except (TypeError, ValueError) as exc:
        raise RegistryError("Invalid frozen training configuration") from exc
    if (checked.horizon_ns != model.horizon_ns or checked.max_label_delay_ns != model.max_label_delay_ns
            or checked.max_abs_prediction_bps != model.max_abs_prediction_bps
            or checked.seed != model.training_metadata.get("seed")
            or configuration.get("features") != asdict(model.feature_config)):
        raise RegistryError("Frozen training configuration does not match its estimator")
    return ["--horizon-ns", str(checked.horizon_ns), "--max-label-delay-ns", str(checked.max_label_delay_ns),
            "--window-events", str(model.feature_config.window_events), "--max-gap-ns", str(model.feature_config.max_gap_ns),
            "--regular-open-ns", str(model.feature_config.regular_open_ns),
            "--regular-close-ns", str(model.feature_config.regular_close_ns),
            "--min-samples-per-session", str(checked.min_samples_per_session),
            "--max-abs-prediction-bps", str(checked.max_abs_prediction_bps),
            "--seed", str(checked.seed), "--alphas", *map(str, checked.alphas)]


def migrate_attempts(store):
    """Explicitly upgrade v1 only from fully registered immutable candidates.

    Unknown, failed or unregistered attempts are not guessed. Their old ledger
    stays intact for operator incident review. The exact old bytes are backed up.
    """
    registry = Registry(store, create=False)
    path = registry.root / "retrain" / "attempts.json"
    with registry._lock():
        if not _present(path):
            return {"schema_version": 2, "state": "absent", "migrated": False}
        raw = read_bytes(path)
        previous = _decode(raw)
        if previous.get("schema_version") == 2:
            if not isinstance(previous.get("datasets"), dict) or not isinstance(previous.get("holdout_keys"), dict):
                raise RegistryError("Malformed v2 retraining ledger")
            return {"schema_version": 2, "state": "already_current", "migrated": False,
                    "datasets": len(previous["datasets"])}
        datasets = previous.get("datasets")
        if previous.get("schema_version") != 1 or not isinstance(datasets, dict) or len(datasets) > 1000:
            raise RegistryError("Unsupported legacy retraining ledger")
        holdout_keys = {}
        for dataset_sha256, request_id in datasets.items():
            if (not isinstance(dataset_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", dataset_sha256)
                    or not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{24}", request_id)):
                raise RegistryError("Invalid legacy attempt identity")
            request = read_json(registry.root / "retrain" / "requests" / f"{request_id}.json")
            if request.get("request_id") != request_id or request.get("dataset_sha256") != dataset_sha256:
                raise RegistryError("Legacy request does not bind its attempted dataset")
            model_bytes = read_bytes(registry.root / "retrain" / "runs" / request_id / "training" / "model.json")
            trained = LinearModel.from_bytes(model_bytes)
            manifest = registry._manifest(trained.model_version)
            registry._check_model(trained.model_version, manifest)
            if (hashlib.sha256(model_bytes).hexdigest() != manifest["model_sha256"]
                    or dataset_sha256 != manifest["dataset_sha256"]
                    or dataset_sha256 != trained.training_metadata.get("dataset_sha256")
                    or manifest["data_manifest_sha256"] != request.get("data_manifest_sha256")
                    or manifest["data_manifest_sha256"] != trained.training_metadata.get("data_manifest_sha256")
                    or manifest["admission_sha256"] != request.get("admission_sha256")):
                raise RegistryError("Legacy run is not the exact registered dataset/model/admission")
            captures = {capture["capture_id"]: capture for capture in manifest["captures"]}
            partitions = trained.training_metadata.get("split", {})
            if not isinstance(partitions, dict) or set(partitions) != {"train", "validation", "test"}:
                raise RegistryError("Legacy candidate lacks a complete immutable partition")
            seen, test_keys = set(), set()
            for region in ("train", "validation", "test"):
                if not isinstance(partitions[region], dict):
                    raise RegistryError("Legacy candidate partition is malformed")
                sessions = partitions[region].get("sessions")
                if not isinstance(sessions, list) or not sessions:
                    raise RegistryError("Legacy candidate partition is incomplete")
                for session in sessions:
                    if not isinstance(session, dict):
                        raise RegistryError("Legacy candidate session is malformed")
                    capture = captures.get(session.get("capture_id"))
                    if capture is None or capture["capture_id"] in seen or session.get("session_date") != capture["session_date"]:
                        raise RegistryError("Legacy candidate partition/source identity mismatch")
                    seen.add(capture["capture_id"])
                    if region == "test":
                        test_keys.add("sha256:" + capture["itch_sha256"])
                        test_keys.add("session:" + manifest["symbol"] + ":" + capture["session_date"])
            if seen != set(captures) or not test_keys:
                raise RegistryError("Legacy candidate does not exhaustively bind its dataset")
            if any(key in holdout_keys and holdout_keys[key] != dataset_sha256 for key in test_keys):
                raise RegistryError("Legacy attempts reused a holdout; operator incident review is required")
            holdout_keys.update({key: dataset_sha256 for key in test_keys})
        if "holdout_keys" in previous and previous["holdout_keys"] != holdout_keys:
            raise RegistryError("Legacy reserved holdout keys disagree with registered evidence")
        if len(holdout_keys) > 10000:
            raise RegistryError("Migrated holdout ledger would exceed its bounded capacity")
        identity = hashlib.sha256(raw).hexdigest()
        backup = path.parent / f"attempts.v1.{identity}.json"
        if _present(backup):
            if read_bytes(backup) != raw:
                raise RegistryError("Existing migration backup is inconsistent")
        else:
            _write(backup, raw, raw=True)
        upgraded = {"schema_version": 2, "datasets": datasets, "holdout_keys": holdout_keys,
                    "migration": {"from_schema_version": 1, "source_sha256": identity,
                                  "backup": backup.name, "migrated_at_ns": time.time_ns()}}
        _write(path, upgraded, replace=True)
        return {"schema_version": 2, "state": "migrated", "migrated": True, "datasets": len(datasets),
                "reserved_holdout_keys": len(holdout_keys), "backup": backup.name, "source_sha256": identity}


def tick(store, *, base_version, data_manifest=None, feature_window=None, exporter=None,
         config=None, execute=False, allow_synthetic=False, now_ns=None):
    """A synchronous operator tick; cron overlap is serialized by the writer lock."""
    config = (config or RetrainConfig()).validate()
    now = time.time_ns() if now_ns is None else now_ns
    if type(now) is not int or now <= 0 or type(execute) is not bool or type(allow_synthetic) is not bool:
        raise RegistryError("Invalid scheduler clock")
    registry = Registry(store)
    # Holding the lock bounds concurrent training to one subprocess per store.
    # Read-only inference never takes this lock and cannot be blocked by training.
    with registry._lock():
        manifest = registry._manifest(base_version)
        model = registry._check_model(base_version, manifest)
        frozen_arguments = _frozen_training_arguments(model)
        schedule_path = registry.root / "retrain" / "schedule.json"
        schedule = read_json(schedule_path) if _present(schedule_path) else {"schema_version": 1, "last_completed_at_ns": manifest["registered_at_ns"]}
        completed = schedule.get("last_completed_at_ns")
        if schedule.get("schema_version") != 1 or type(completed) is not int or completed > now:
            raise RegistryError("Invalid scheduler history or regressed clock")
        drift = _window(feature_window, model, config, now) if feature_window else None
        due = now - completed >= config.interval_seconds * 1_000_000_000
        shifted = bool(drift and drift["evaluated"] and drift["drift_detected"])
        if not due and not shifted:
            return _latest(registry, "insufficient_data" if drift and not drift["evaluated"] else "idle",
                           "INSUFFICIENT_WINDOW" if drift and not drift["evaluated"] else "NOT_DUE", now=now)
        reason = "FEATURE_DRIFT" if shifted else "PERIODIC"
        if data_manifest is None:
            return _latest(registry, "skipped", "NO_NEW_AUTHORIZED_DATA", now=now, extra={"trigger": reason})
        try:
            data = load_manifest(data_manifest)
        except (OSError, ValueError):
            return _latest(registry, "blocked", "DATA_INVALID", now=now)
        if (data.provenance["kind"] == "synthetic" and not allow_synthetic
                or data.provenance["kind"] == "licensed_market" and not data.provenance["authorized_use_attested"]):
            return _latest(registry, "blocked", "NO_NEW_AUTHORIZED_DATA", now=now)
        if data.dataset_sha256 == manifest["dataset_sha256"]:
            return _latest(registry, "skipped", "DATA_ALREADY_ATTEMPTED", now=now)
        if len(data.captures) < 10:
            return _latest(registry, "insufficient_data", "NO_NEW_AUTHORIZED_DATA", now=now)
        if any(c.symbol != manifest["symbol"] for c in data.captures):
            return _latest(registry, "blocked", "SYMBOL_MISMATCH", now=now)
        # Do not repeatedly tune on a previously consumed test source or session.
        try:
            check_available(registry, data)
        except ReservationConflict:
            return _latest(registry, "blocked", "DATA_ALREADY_ATTEMPTED", now=now)
        test_captures = data.captures[int(len(data.captures) * 0.8):]
        keys = {"sha256:" + c.itch_sha256 for c in test_captures}
        keys |= {"session:" + c.symbol + ":" + c.session_date for c in test_captures}
        attempts_path = registry.root / "retrain" / "attempts.json"
        attempts = read_json(attempts_path) if _present(attempts_path) else {"schema_version": 2, "datasets": {}, "holdout_keys": {}}
        if attempts.get("schema_version") == 1:
            raise RegistryError("Legacy retraining ledger requires an explicit --migrate-attempts upgrade")
        if (attempts.get("schema_version") != 2 or not isinstance(attempts.get("datasets"), dict)
                or not isinstance(attempts.get("holdout_keys"), dict) or len(attempts["datasets"]) >= 1000
                or len(attempts["holdout_keys"]) + len(keys) > 10000):
            raise RegistryError("Invalid or full retraining ledger")
        if data.dataset_sha256 in attempts["datasets"] or keys.intersection(attempts["holdout_keys"]):
            return _latest(registry, "skipped", "DATA_ALREADY_ATTEMPTED", now=now)
        request_id = hashlib.sha256(canonical_bytes({"base": base_version, "dataset": data.dataset_sha256,
                                                     "admission": manifest["admission_sha256"]})).hexdigest()[:24]
        request = {"schema_version": 1, "request_id": request_id, "base_version": base_version,
                   "dataset_sha256": data.dataset_sha256, "data_manifest_sha256": data.manifest_sha256,
                   "admission_sha256": manifest["admission_sha256"], "reason": reason, "created_at_ns": now,
                   "state": "requested", "drift": drift, "automatic_promotion": False,
                   "scheduler_config": asdict(config)}
        request_path = registry.root / "retrain" / "requests" / f"{request_id}.json"
        if not _present(request_path):
            _write(request_path, request)
        else:
            existing = read_json(request_path)
            if any(existing.get(key) != request[key] for key in
                   ("schema_version", "request_id", "base_version", "dataset_sha256", "admission_sha256")):
                raise RegistryError("Existing retraining request does not bind this candidate")
        if not execute:
            return _latest(registry, "requested", "DRY_RUN", now=now, extra={"request_id": request_id})
        shared_request_sha = hashlib.sha256(canonical_bytes({"request_id": request_id, "base_version": base_version,
            "dataset_sha256": data.dataset_sha256, "manifest_sha256": data.manifest_sha256,
            "admission_sha256": manifest["admission_sha256"], "training_config": model.training_metadata["config"]})).hexdigest()
        reserve(registry, data, owner="retrain", request_sha256=shared_request_sha, now_ns=now)
        attempts["datasets"][data.dataset_sha256] = request_id
        # Reserve source/session identities before the process starts. This
        # survives a crash before registration and protects overlapping datasets
        # during the short handoff between training and candidate registration.
        attempts["holdout_keys"].update({key: data.dataset_sha256 for key in keys})
        _write(attempts_path, attempts, replace=True)
        run_dir = registry.root / "retrain" / "runs" / request_id
        _mkdir(run_dir)
        output_dir = run_dir / "training"
        command = [sys.executable, "-m", "ml.train", "--manifest", str(data.path), "--output", str(output_dir), *frozen_arguments]
        if exporter:
            command += ["--exporter", str(Path(exporter).absolute())]
        _write(run_dir / "invocation.json", {"schema_version": 1, "command": command, "timeout_seconds": config.timeout_seconds})
        _latest(registry, "running", reason, now=now, extra={"request_id": request_id})
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PYTHONPATH": str(ROOT),
                       "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
        try:
            out_fd = os.open(run_dir / "stdout.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            err_fd = os.open(run_dir / "stderr.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(out_fd, "wb") as stdout, os.fdopen(err_fd, "wb") as stderr:
                subprocess.run(command, cwd=ROOT, env=environment, stdout=stdout, stderr=stderr,
                               timeout=config.timeout_seconds, check=True)
            trained = LinearModel.load(output_dir / "model.json")
            if (trained.training_metadata.get("dataset_sha256") != data.dataset_sha256
                    or trained.training_metadata.get("config") != model.training_metadata.get("config")):
                raise RegistryError("Trainer returned an unrelated dataset or changed frozen configuration")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            _write(run_dir / "failure.json", {"schema_version": 1, "stage": "training", "error_type": type(exc).__name__,
                                              "returncode": getattr(exc, "returncode", None), "failed_at_ns": time.time_ns()})
            return _latest(registry, "failed", "TRAINING_FAILED", now=time.time_ns(), extra={"request_id": request_id})
    # Release the training writer lock before registration acquires it.
    try:
        version = registry.register(output_dir / "model.json", admission_sha256=manifest["admission_sha256"], data_manifest=data.path)
    except (OSError, ValueError) as exc:
        with registry._lock():
            _write(run_dir / "failure.json", {"schema_version": 1, "stage": "registration", "error_type": type(exc).__name__,
                                              "failed_at_ns": time.time_ns()})
            return _latest(registry, "failed", "TRAINING_FAILED", now=time.time_ns(), extra={"request_id": request_id})
    with registry._lock():
        completed_at = time.time_ns()
        _write(schedule_path, {"schema_version": 1, "last_completed_at_ns": completed_at}, replace=True)
        return _latest(registry, "completed", "CREATED_CANDIDATE", now=completed_at,
                       extra={"request_id": request_id, "model_version": version, "automatic_promotion": False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--base-version")
    parser.add_argument("--migrate-attempts", action="store_true", help="Verify registered v1 attempts and upgrade their reserved holdout ledger")
    parser.add_argument("--manifest")
    parser.add_argument("--window")
    parser.add_argument("--exporter")
    parser.add_argument("--interval-seconds", type=int, default=86400)
    parser.add_argument("--min-samples", type=int, default=200)
    parser.add_argument("--mean-z-threshold", type=float, default=2.0)
    parser.add_argument("--psi-threshold", type=float, default=0.2)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--execute", action="store_true", help="Actually invoke the offline trainer; never activates a model")
    parser.add_argument("--allow-synthetic", action="store_true", help="Research candidates only; can never grant broker paper")
    args = parser.parse_args(argv)
    try:
        if args.migrate_attempts:
            if args.base_version or args.manifest or args.window or args.execute:
                raise RegistryError("Ledger migration cannot be combined with a training tick")
            print(json.dumps(migrate_attempts(args.store), allow_nan=False))
            return 0
        if not args.base_version:
            raise RegistryError("--base-version is required for a retraining tick")
        result = tick(args.store, base_version=args.base_version, data_manifest=args.manifest,
                      feature_window=args.window, exporter=args.exporter,
                      config=RetrainConfig(args.interval_seconds, args.min_samples, args.mean_z_threshold,
                                           args.psi_threshold, args.timeout_seconds),
                      execute=args.execute, allow_synthetic=args.allow_synthetic)
        print(json.dumps(result, allow_nan=False))
        return 2 if result["state"] in {"failed", "blocked"} else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"state": "blocked", "reason": "CONFIG_INVALID", "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
