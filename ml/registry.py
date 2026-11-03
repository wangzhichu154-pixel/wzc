"""Local immutable JSON model registry and explicit, fail-closed promotion gates.

The registry is an operator-owned filesystem trust boundary, not a remote signing
service. No web endpoint may write here. Loading a model never grants execution.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

from .model import LinearModel, canonical_bytes
from .valuation import aggregate_sessions, evaluation_contract, research_source_fingerprints

VERSION_RE = re.compile(r"ridge-[0-9a-f]{24}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
STAGES = ("candidate", "evaluated", "simulation_validated", "operator_paper_approved")
POLICY_FIELDS = {"signal_ttl_ns", "min_expected_edge_bps", "fee_per_share_units",
                 "min_fee_per_order_units", "slippage_buffer_units", "max_target_position", "allow_short"}
DEFAULT_GATES = {
    "min_train_samples": 200, "min_validation_samples": 100,
    "max_validation_rmse_bps": 5.0, "min_rmse_improvement_ratio": 0.0,
    "min_direction_accuracy": 0.5, "min_simulation_events": 1000,
    "min_simulation_fills": 10, "min_net_pnl_units": 0,
    "max_drawdown_units": 1_000_000, "min_stress_net_pnl_units": -50_000,
    "required_stress_multiplier": 2.0, "min_holdout_sessions": 1,
    "max_model_age_seconds": 604800,
}
_READ_BUDGET = ContextVar("registry_read_budget", default=None)
FAILURE_CODES = frozenset({
    "INVALID_EVIDENCE", "TRAINING_NOT_ACCEPTED", "TRAINING_GATE_REJECTED",
    "LEGACY_REPORT_REQUIRES_REVALIDATION", "LEGACY_FAILURE_UNCLASSIFIED",
    "HOLDOUT_MISMATCH", "AGGREGATE_MISMATCH", "NO_FILLS", "INSUFFICIENT_FILLS",
    "INSUFFICIENT_EVENTS", "INCOMPLETE_VALUATION", "INCOMPLETE_DRAWDOWN",
    "OPEN_EXPOSURE", "RISK_KILLED", "NET_PNL_BELOW_LIMIT", "DRAWDOWN_LIMIT",
    "STRESS_NET_PNL_BELOW_LIMIT", "MISSING_COST_STRESS",
})


class RegistryError(ValueError):
    def __init__(self, message, *, failure_codes=None):
        super().__init__(message)
        codes = tuple(dict.fromkeys(failure_codes or ("INVALID_EVIDENCE",)))
        if any(code not in FAILURE_CODES for code in codes):
            raise ValueError("Unsupported registry failure code")
        self.failure_codes = codes


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("Duplicate JSON key")
        result[key] = value
    return result


def _decode(raw: bytes):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(RegistryError("Nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RegistryError("Invalid JSON document") from exc
    if not isinstance(value, dict):
        raise RegistryError("Expected a JSON object")
    return value


def _path(path) -> Path:
    value = Path(path).absolute()
    if ".." in value.parts:
        raise RegistryError("Parent traversal is forbidden")
    return value


def _present(path):
    return path.exists() or path.is_symlink()


def _open(path, *, directory=False):
    """Walk each path component using openat + O_NOFOLLOW (Linux/POSIX)."""
    value = _path(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in value.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else os.O_NONBLOCK)
        result = os.open(value.name, flags, dir_fd=descriptor)
        if directory:
            return result
        info = os.fstat(result)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(info.st_mode) & 0o022):
            os.close(result)
            raise RegistryError("A regular file is required")
        return result
    except OSError as exc:
        raise RegistryError("Missing, unsafe or unreadable registry path") from exc
    finally:
        os.close(descriptor)


def read_json(path, *, limit=2 * 1024 * 1024):
    return _decode(read_bytes(path, limit=limit))


def read_bytes(path, *, limit=2 * 1024 * 1024):
    budget = _READ_BUDGET.get()
    if budget is not None:
        if budget[1] <= 0 or budget[0] <= 0:
            raise RegistryError("Registry status read budget exceeded")
        budget[1] -= 1
        limit = min(limit, budget[0])
    descriptor = _open(path)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise RegistryError("Document exceeds size bound")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise RegistryError("Document exceeds size bound")
    if budget is not None:
        budget[0] -= len(data)
    return data


def _mkdir(path):
    value = _path(path)
    current = Path("/")
    for component in value.parts[1:]:
        current = current / component
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        descriptor = _open(current, directory=True)
        os.close(descriptor)


def _sync_dir(path):
    descriptor = _open(path, directory=True)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _trusted_directory(path):
    descriptor = _open(path, directory=True)
    try:
        info = os.fstat(descriptor)
        if info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022:
            raise RegistryError("Registry directories must be operator/root owned and not publicly writable")
    finally:
        os.close(descriptor)


def _write(path, value, *, replace=False, raw=False):
    path = _path(path)
    _mkdir(path.parent)
    if path.is_symlink():
        raise RegistryError("Symbolic destination is forbidden")
    data = value if raw else canonical_bytes(value) + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o600 if replace else 0o444)
        if replace:
            os.replace(temporary, path)
        else:
            # link is atomic and refuses an existing destination; never overwrite evidence.
            os.link(temporary, path, follow_symlinks=False)
            os.unlink(temporary)
        _sync_dir(path.parent)
    except FileExistsError as exc:
        raise RegistryError("Immutable evidence already exists") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def _number(value, name, *, low=None, high=None, integer=False):
    if (type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value)
            or (low is not None and value < low) or (high is not None and value > high)):
        raise RegistryError(f"Invalid numeric field: {name}")
    return value


def validate_admission(document):
    if not isinstance(document, dict) or set(document) != {"schema_version", "policy", "gates"} or document["schema_version"] != 1:
        raise RegistryError("Admission requires schema_version, policy and explicit gates")
    policy = document["policy"]
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        raise RegistryError("Policy must use the canonical runtime fields, excluding clock")
    for name in POLICY_FIELDS - {"allow_short", "min_expected_edge_bps"}:
        _number(policy[name], name, low=1 if name in {"signal_ttl_ns", "max_target_position"} else 0,
                high=60_000_000_000, integer=True)
    _number(policy["min_expected_edge_bps"], "min_expected_edge_bps", low=0, high=10000)
    if type(policy["allow_short"]) is not bool:
        raise RegistryError("allow_short must be a boolean")
    gates = document["gates"]
    if not isinstance(gates, dict) or set(gates) != set(DEFAULT_GATES):
        raise RegistryError("All admission gates must be explicitly predeclared")
    for name, value in gates.items():
        _number(value, name)
    for name in ("min_train_samples", "min_validation_samples", "min_simulation_events", "min_simulation_fills",
                 "min_holdout_sessions", "max_model_age_seconds"):
        _number(gates[name], name, low=1, high=100_000_000, integer=True)
    _number(gates["max_validation_rmse_bps"], "max_validation_rmse_bps", low=0)
    _number(gates["min_rmse_improvement_ratio"], "min_rmse_improvement_ratio", low=0, high=1)
    _number(gates["min_direction_accuracy"], "min_direction_accuracy", low=0, high=1)
    _number(gates["required_stress_multiplier"], "required_stress_multiplier", low=1, high=100)
    _number(gates["max_drawdown_units"], "max_drawdown_units", low=0, integer=True)
    for name in ("min_net_pnl_units", "min_stress_net_pnl_units"):
        _number(gates[name], name, integer=True)
    return document


@dataclass(frozen=True)
class ApprovedModel:
    path: Path
    version: str
    sha256: str
    manifest: dict


class Registry:
    def __init__(self, store, *, create=True):
        self.root = _path(store)
        if create:
            _mkdir(self.root)
            _trusted_directory(self.root)
            for name in ("versions", "policies", "events", "evidence", "history", "retrain"):
                _mkdir(self.root / name)
        else:
            descriptor = _open(self.root, directory=True)
            os.close(descriptor)
        _trusted_directory(self.root)
        for name in ("versions", "policies", "events", "evidence", "history", "retrain"):
            if _present(self.root / name):
                _trusted_directory(self.root / name)

    @contextmanager
    def _lock(self):
        path = self.root / ".writer.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RegistryError("Invalid registry lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def install_policy(self, admission):
        document = read_json(admission) if isinstance(admission, (str, Path)) else admission
        validate_admission(document)
        identity = digest(document)
        with self._lock():
            destination = self.root / "policies" / f"{identity}.json"
            if _present(destination):
                existing = read_json(destination)
                if existing.get("admission") != document:
                    raise RegistryError("Policy identity conflict")
            else:
                _write(destination, {"schema_version": 1, "admission": document,
                                     "registered_at_ns": time.time_ns(), "admission_sha256": identity})
        return identity

    def _policy(self, identity):
        if not isinstance(identity, str) or not HASH_RE.fullmatch(identity):
            raise RegistryError("Invalid policy identity")
        stored = read_json(self.root / "policies" / f"{identity}.json")
        document = validate_admission(stored.get("admission"))
        if digest(document) != identity or stored.get("admission_sha256") != identity:
            raise RegistryError("Policy hash mismatch")
        return stored

    def _version(self, version):
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise RegistryError("Invalid model version")
        path = self.root / "versions" / version
        if _present(path):
            _trusted_directory(path)
        return path

    def _manifest(self, version):
        manifest = read_json(self._version(version) / "manifest.json", limit=256 * 1024)
        if manifest.get("schema_version") != 1 or manifest.get("version") != version:
            raise RegistryError("Invalid model manifest")
        stored = self._policy(manifest.get("admission_sha256"))
        if (manifest.get("policy") != stored["admission"]["policy"]
                or manifest.get("gates") != stored["admission"]["gates"]
                or manifest.get("policy_sha256") != digest(manifest["policy"])
                or manifest.get("gates_sha256") != digest(manifest["gates"])):
            raise RegistryError("Manifest policy binding mismatch")
        return manifest

    def register(self, model_path, *, admission_sha256, data_manifest):
        from .data import load_manifest
        raw = read_bytes(model_path)
        model = LinearModel(_decode(raw))
        data = load_manifest(data_manifest)
        if len(data.captures) > 512:
            raise RegistryError("Registry supports at most 512 sessions per version")
        if model.provenance.get("kind") != data.provenance.get("kind"):
            raise RegistryError("Model/data provenance mismatch")
        if model.training_metadata.get("data_manifest_sha256") != data.manifest_sha256:
            raise RegistryError("Model training manifest binding mismatch")
        if model.training_metadata.get("dataset_sha256") != data.dataset_sha256:
            raise RegistryError("Model dataset identity mismatch")
        policy = self._policy(admission_sha256)
        config = policy["admission"]["policy"]
        if config["signal_ttl_ns"] > model.horizon_ns:
            raise RegistryError("Signal TTL exceeds model horizon")
        symbols = {capture.symbol for capture in data.captures}
        if len(symbols) != 1:
            raise RegistryError("Only a single-symbol model can be registered")
        manifest = {
            "schema_version": 1, "version": model.model_version,
            "model_sha256": hashlib.sha256(raw).hexdigest(), "registered_at_ns": time.time_ns(),
            "provenance_kind": data.provenance["kind"],
            "authorized_use_attested": data.provenance.get("authorized_use_attested") is True,
            "symbol": next(iter(symbols)), "target_horizon_ns": model.horizon_ns,
            "dataset_sha256": data.dataset_sha256, "data_manifest_sha256": data.manifest_sha256,
            "policy": config, "policy_sha256": digest(config), "gates": policy["admission"]["gates"],
            "gates_sha256": digest(policy["admission"]["gates"]), "admission_sha256": admission_sha256,
            "policy_registered_at_ns": policy["registered_at_ns"],
            "policy_clock_contract": "paper:monotonic;simulation:exchange_logical",
            "captures": [{"capture_id": c.capture_id, "session_date": c.session_date,
                          "itch_sha256": c.itch_sha256, "market_csv_sha256": c.market_csv_sha256} for c in data.captures],
        }
        version = model.model_version
        with self._lock():
            destination = self._version(version)
            if _present(destination):
                existing = self._manifest(version)
                if (existing["model_sha256"] != manifest["model_sha256"]
                        or existing["admission_sha256"] != admission_sha256
                        or existing["data_manifest_sha256"] != data.manifest_sha256):
                    raise RegistryError("Existing immutable model has different bindings")
                self._check_model(version, existing)
                return version
            temporary = Path(tempfile.mkdtemp(prefix=".candidate-", dir=destination.parent))
            try:
                _write(temporary / "model.json", raw, raw=True)
                _write(temporary / "manifest.json", manifest)
                os.rename(temporary, destination)
                _sync_dir(destination.parent)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            self._update_index(version, "candidate")
        return version

    def _check_model(self, version, manifest=None):
        manifest = manifest or self._manifest(version)
        raw = read_bytes(self._version(version) / "model.json")
        if hashlib.sha256(raw).hexdigest() != manifest["model_sha256"]:
            raise RegistryError("Model file hash mismatch")
        model = LinearModel(_decode(raw))
        if model.model_version != version or model.horizon_ns != manifest["target_horizon_ns"]:
            raise RegistryError("Model identity mismatch")
        return model

    def _event(self, version, stage):
        if stage not in STAGES[1:]:
            raise RegistryError("Unknown promotion stage")
        self._version(version)
        _trusted_directory(self.root / "events" / version)
        event = read_json(self.root / "events" / version / f"{stage}.json", limit=256 * 1024)
        manifest = self._manifest(version)
        if (event.get("schema_version") != 1 or event.get("stage") != stage
                or event.get("version") != version or event.get("model_sha256") != manifest["model_sha256"]
                or event.get("admission_sha256") != manifest["admission_sha256"]):
            raise RegistryError("Promotion event binding mismatch")
        report_hash = event.get("report_sha256")
        if not isinstance(report_hash, str) or not HASH_RE.fullmatch(report_hash):
            raise RegistryError("Invalid report identity")
        report = read_json(self.root / "evidence" / f"{report_hash}.json")
        if digest(report) != report_hash:
            raise RegistryError("Promotion evidence hash mismatch")
        return event, report

    def _record(self, version, stage, report, *, extra=None):
        manifest = self._manifest(version)
        report_hash = digest(report)
        evidence = self.root / "evidence" / f"{report_hash}.json"
        if not _present(evidence):
            _write(evidence, report)
        elif digest(read_json(evidence)) != report_hash:
            raise RegistryError("Evidence hash conflict")
        event = {"schema_version": 1, "version": version, "stage": stage,
                 "model_sha256": manifest["model_sha256"], "admission_sha256": manifest["admission_sha256"],
                 "report_sha256": report_hash, "recorded_at_ns": time.time_ns(), **(extra or {})}
        destination = self.root / "events" / version / f"{stage}.json"
        if _present(destination):
            previous, _ = self._event(version, stage)
            if previous["report_sha256"] != report_hash:
                raise RegistryError("A promotion stage cannot be rewritten with new evidence")
            return previous
        _write(destination, event)
        self._update_index(version, stage)
        return event

    def _update_index(self, version, stage):
        path = self.root / "index.json"
        index = read_json(path, limit=128 * 1024) if _present(path) else {"schema_version": 1, "models": []}
        models = index.get("models")
        if index.get("schema_version") != 1 or not isinstance(models, list) or len(models) > 100:
            raise RegistryError("Invalid bounded model index")
        models = [item for item in models if isinstance(item, dict) and item.get("version") != version]
        models.insert(0, {"version": version, "stage": stage})
        _write(path, {"schema_version": 1, "models": models[:100]}, replace=True)

    def _validate_training(self, version, report):
        manifest = self._manifest(version)
        model = self._check_model(version, manifest)
        if (model.training_metadata.get("market_exports_verified") is not True
                or report.get("market_exports_verified") is not True
                or not HASH_RE.fullmatch(str(model.training_metadata.get("exporter_sha256", "")))
                or report.get("exporter_sha256") != model.training_metadata["exporter_sha256"]):
            raise RegistryError("Verified C++ market export lineage is required")
        if (report.get("schema_version") != 1 or report.get("model_version") != version
                or report.get("model_sha256") != manifest["model_sha256"]
                or report.get("dataset_sha256") != manifest["dataset_sha256"]
                or report.get("data_manifest_sha256") != manifest["data_manifest_sha256"]
                or report.get("provenance", {}).get("kind") != manifest["provenance_kind"]):
            raise RegistryError("Training report identity/provenance mismatch")
        if report.get("chronological_split") is not True or report.get("purged_horizon") is not True:
            raise RegistryError("Chronological purged evaluation is required")
        splits = report.get("split", {})
        if set(splits) != {"train", "validation", "test"} or splits != model.training_metadata.get("split"):
            raise RegistryError("Training split differs from immutable model metadata")
        capture_map = {item["capture_id"]: item for item in manifest["captures"]}
        seen, boundaries = set(), {}
        for region in ("train", "validation", "test"):
            group = splits[region]
            samples = _number(group.get("samples"), "split.samples", low=1, integer=True)
            sessions = group.get("sessions")
            if not isinstance(sessions, list) or not sessions:
                raise RegistryError("Each split requires independent dated sessions")
            count, starts, ends = 0, [], []
            for item in sessions:
                ident = item.get("capture_id")
                if ident not in capture_map or ident in seen or item.get("session_date") != capture_map[ident]["session_date"]:
                    raise RegistryError("Split overlap or unknown session")
                seen.add(ident)
                day = date.fromisoformat(item["session_date"]).toordinal() * 86_400_000_000_000
                start = _number(item.get("first_timestamp_ns"), "first_timestamp_ns", low=0, high=86_400_000_000_000 - 1, integer=True)
                end = _number(item.get("last_timestamp_ns"), "last_timestamp_ns", low=start, high=86_400_000_000_000 - 1, integer=True)
                label_end = _number(item.get("last_label_end_ns"), "last_label_end_ns", low=end + model.horizon_ns,
                                    high=86_400_000_000_000 - 1, integer=True)
                count += _number(item.get("samples"), "session.samples", low=1, integer=True)
                starts.append(day + start)
                ends.append(day + label_end)
            if count != samples:
                raise RegistryError("Split sample count mismatch")
            boundaries[region] = (min(starts), max(ends))
        if seen != set(capture_map):
            raise RegistryError("Dataset captures are not exhaustively bound to splits")
        purge = report.get("purge", {})
        if (purge.get("horizon_ns") != model.horizon_ns or purge.get("max_label_delay_ns") != model.max_label_delay_ns
                or purge.get("label_cross_session_allowed") is not False or purge.get("label_cross_reset_allowed") is not False):
            raise RegistryError("Unsupported label/purge contract")
        embargo = _number(purge.get("embargo_ns"), "embargo_ns", low=model.horizon_ns, integer=True)
        if not (boundaries["train"][1] + embargo < boundaries["validation"][0]
                and boundaries["validation"][1] + embargo < boundaries["test"][0]):
            raise RegistryError("Chronological splits overlap labels or embargo")
        holdout = report.get("holdout", {})
        expected_test_hashes = {capture_map[item["capture_id"]]["itch_sha256"] for item in splits["test"]["sessions"]}
        if (holdout.get("locked") is not True or type(holdout.get("reuse_exact_reproduction")) is not bool
                or set(holdout.get("test_source_sha256", [])) != expected_test_hashes
                or not HASH_RE.fullmatch(str(holdout.get("experiment_sha256", "")))):
            raise RegistryError("A locked, source-bound test interval is required")
        if holdout["experiment_sha256"] != digest({k: v for k, v in model.to_dict().items() if k != "model_version"}):
            raise RegistryError("Holdout experiment does not bind the exact estimator")
        gates = manifest["gates"]
        if splits["train"]["samples"] < gates["min_train_samples"] or splits["validation"]["samples"] < gates["min_validation_samples"]:
            raise RegistryError("Insufficient training or validation samples")
        metrics = report.get("metrics", {}).get("validation", {})
        ridge, baseline = metrics.get("ridge", {}), metrics.get("zero", {})
        if ridge.get("samples") != splits["validation"]["samples"] or baseline.get("samples") != ridge["samples"]:
            raise RegistryError("Validation metric sample binding mismatch")
        rmse = _number(ridge.get("rmse_bps"), "rmse_bps", low=0)
        zero = _number(baseline.get("rmse_bps"), "baseline.rmse_bps", low=0)
        accuracy = _number(ridge.get("direction_accuracy"), "direction_accuracy", low=0, high=1)
        improvement = (zero - rmse) / zero if zero > 0 else (-1.0 if rmse > 0 else 0.0)
        if (rmse > gates["max_validation_rmse_bps"] or accuracy < gates["min_direction_accuracy"]
                or improvement < gates["min_rmse_improvement_ratio"]):
            raise RegistryError("Predeclared chronological validation thresholds not met", failure_codes=["TRAINING_GATE_REJECTED"])
        trained_at = _number(report.get("trained_at_ns"), "trained_at_ns", low=1, integer=True)
        completed_at = _number(report.get("completed_at_ns"), "completed_at_ns", low=trained_at, integer=True)
        if completed_at > time.time_ns() + 1_000_000_000:
            raise RegistryError("Training report is from the future")
        return trained_at >= manifest["policy_registered_at_ns"]

    def evaluate(self, version, training_report):
        report = read_json(training_report) if isinstance(training_report, (str, Path)) else training_report
        with self._lock():
            try:
                precommitted = self._validate_training(version, report)
                self._claim_holdout(version, report)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                codes = getattr(exc, "failure_codes", ("INVALID_EVIDENCE",))
                self._record_attempt(version, "training", report, False, failure_codes=codes)
                raise RegistryError(str(exc), failure_codes=codes) from exc
            self._record_attempt(version, "training", report, True)
            return self._record(version, "evaluated", report, extra={"policy_precommitted": precommitted})

    def _claim_holdout(self, version, report):
        path = self.root / "holdouts.json"
        ledger = read_json(path) if _present(path) else {"schema_version": 1, "entries": {}}
        if ledger.get("schema_version") != 1 or not isinstance(ledger.get("entries"), dict):
            raise RegistryError("Invalid holdout ledger")
        manifest = self._manifest(version)
        keys = ["sha256:" + x for x in report["holdout"]["test_source_sha256"]]
        keys += ["session:" + manifest["symbol"] + ":" + x["session_date"] for x in report["split"]["test"]["sessions"]]
        experiment = report["holdout"]["experiment_sha256"]
        if any(key in ledger["entries"] and ledger["entries"][key] != experiment for key in keys):
            raise RegistryError("Locked test source/session was used by a different experiment")
        if len(ledger["entries"]) + len(keys) > 10000:
            raise RegistryError("Holdout ledger capacity requires operator maintenance")
        ledger["entries"].update({key: experiment for key in keys})
        _write(path, ledger, replace=True)

    def _record_attempt(self, version, kind, report, accepted, *, failure_codes=()):
        manifest = self._manifest(version)
        identity = digest(report)
        destination = self.root / "evidence" / f"{identity}.json"
        if len(canonical_bytes(report)) > 2 * 1024 * 1024:
            raise RegistryError("Evaluation evidence exceeds size bound")
        if not _present(destination):
            _write(destination, report)
        pointer = {"schema_version": 1, "version": version, "kind": kind, "accepted": accepted,
                   "model_sha256": manifest["model_sha256"], "report_sha256": identity,
                   "updated_at_ns": time.time_ns(), "failure_codes": list(failure_codes)}
        _write(self.root / "events" / version / f"{kind}-attempt-{digest(pointer)}.json", pointer)
        _write(self.root / "events" / version / f"latest-{kind}-attempt.json", pointer, replace=True)

    def _scenario(self, rows, reported, *, report, manifest, test, expected_costs, latency_multiplier=1):
        """Reconstruct every aggregate from all locked session engine reports."""
        if not isinstance(rows, list) or len(rows) != len(test):
            raise RegistryError("Complete per-session engine evidence is required", failure_codes=["HOLDOUT_MISMATCH"])
        used = [row.get("capture_id") for row in rows if isinstance(row, dict)]
        if len(used) != len(rows) or set(used) != set(test) or len(set(used)) != len(used):
            raise RegistryError("Per-session evidence does not cover the complete locked test", failure_codes=["HOLDOUT_MISMATCH"])
        plan = report["evaluation_plan"]
        policy = manifest["policy"]
        for row in rows:
            ident = row["capture_id"]
            session = test[ident]
            if row.get("session_date") != session["session_date"]:
                raise RegistryError("Simulation session date differs from locked test")
            start = _number(row.get("start_ns"), "start_ns", low=0, integer=True)
            end = _number(row.get("end_ns"), "end_ns", low=start + 1, high=86_400_000_000_000, integer=True)
            if start > session["first_timestamp_ns"] or end <= session["last_label_end_ns"]:
                raise RegistryError("Simulation window omits part of the locked test session", failure_codes=["HOLDOUT_MISMATCH"])
            engine = row.get("engine_report")
            if not isinstance(engine, dict) or digest(engine) != row.get("engine_report_sha256"):
                raise RegistryError("Raw engine report hash is missing or inconsistent")
            summary = engine.get("summary", {})
            simulation = row.get("simulation")
            if (engine.get("book_valid") is not True or summary.get("mode") != "simulation"
                    or type(summary.get("orders_sent")) is not int or summary["orders_sent"] != 0
                    or type(summary.get("fills_simulated")) is not int
                    or not isinstance(simulation, dict) or summary["fills_simulated"] != simulation.get("fill_count")
                    or simulation != summary.get("simulation")):
                raise RegistryError("Session ledger differs from the raw simulation engine report")
            if (type(row.get("events")) is not int
                    or row["events"] != simulation.get("signal_gate", {}).get("signals_applied")
                    or type(engine.get("messages")) is not int or engine["messages"] < row["events"]):
                raise RegistryError("Reported event count differs from applied engine signals", failure_codes=["AGGREGATE_MISMATCH"])
            config = simulation.get("config", {})
            expected = {**expected_costs, "rebate_per_share_units": 0, "participation_gate_required": True,
                        "long_only": not policy["allow_short"], "max_signal_age_ns": policy["signal_ttl_ns"],
                        "trade_start_ns": start, "trade_end_ns": end,
                        "max_order_size": plan["maximum_order_shares"], "orders_per_second": plan["new_order_rate"],
                        "daily_loss_limit_units": plan["daily_loss_limit_cents"] * 100,
                        "daily_drawdown_limit_units": plan["daily_loss_limit_cents"] * 100,
                        "hard_inventory_limit": policy["max_target_position"],
                        "soft_inventory_limit": max(1, policy["max_target_position"] // 2),
                        "quote_lifetime_ns": plan["quote_lifetime_ns"], "tick_size": plan["tick_size"],
                        "min_net_edge_units": plan["min_net_edge_units"],
                        "latency_ns": plan["latency_ns"] * latency_multiplier,
                        "cancel_latency_ns": plan["cancel_latency_ns"] * latency_multiplier}
            if any(type(config.get(key)) is not type(value) or config[key] != value for key, value in expected.items()):
                raise RegistryError("Actual configured simulation costs, risk or latency differ from the committed policy")
        try:
            computed = aggregate_sessions(rows)
        except (ValueError, KeyError, TypeError) as exc:
            raise RegistryError(f"Session account ledger invalid: {exc}", failure_codes=["AGGREGATE_MISMATCH"]) from exc
        if not isinstance(reported, dict) or canonical_bytes(computed) != canonical_bytes(reported):
            raise RegistryError("Reported aggregate differs from actual per-session account ledgers", failure_codes=["AGGREGATE_MISMATCH"])
        return computed

    @staticmethod
    def _financial_codes(metrics, gates, *, stress=False):
        codes = []
        if not stress:
            if metrics["fills"] == 0:
                codes.append("NO_FILLS")
            elif metrics["fills"] < gates["min_simulation_fills"]:
                codes.append("INSUFFICIENT_FILLS")
            if metrics["events"] < gates["min_simulation_events"]:
                codes.append("INSUFFICIENT_EVENTS")
        valuation = metrics["valuation"]
        if not valuation["net_pnl_valid"]:
            codes.append("INCOMPLETE_VALUATION")
        else:
            floor = gates["min_stress_net_pnl_units"] if stress else gates["min_net_pnl_units"]
            if metrics["net_pnl_units"] < floor:
                codes.append("STRESS_NET_PNL_BELOW_LIMIT" if stress else "NET_PNL_BELOW_LIMIT")
        if not valuation["drawdown_complete"]:
            codes.append("INCOMPLETE_DRAWDOWN")
        elif metrics["max_drawdown_units"] > gates["max_drawdown_units"]:
            codes.append("DRAWDOWN_LIMIT")
        if metrics["killed"]:
            codes.append("RISK_KILLED")
        if metrics["open_exposure"]:
            codes.append("OPEN_EXPOSURE")
        return codes

    def _validate_simulation(self, version, report):
        manifest = self._manifest(version)
        model = self._check_model(version, manifest)
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
                or report.get("schema_version") not in (1, 2) or report.get("report_type") != "simulation_validation"
                or report.get("model_version") != version or report.get("model_sha256") != manifest["model_sha256"]
                or report.get("policy_sha256") != manifest["policy_sha256"]
                or report.get("provenance_kind") != manifest["provenance_kind"]
                or report.get("chronological_holdout") is not True
                or report.get("execution_authorized") is not False):
            raise RegistryError("Simulation report identity/policy binding mismatch")
        if not _present(self.root / "events" / version / "evaluated.json"):
            raise RegistryError("Training must pass before simulation promotion", failure_codes=["TRAINING_NOT_ACCEPTED"])
        _, training = self._event(version, "evaluated")
        self._validate_training(version, training)
        for name in ("input_sha256", "engine_sha256"):
            if not HASH_RE.fullmatch(str(report.get(name, ""))):
                raise RegistryError("Simulation requires exact input and engine hashes")
        if report["engine_sha256"] != model.training_metadata["exporter_sha256"]:
            raise RegistryError("Simulation must use the verified model feature-export engine build")
        used = report.get("capture_ids")
        if not isinstance(used, list) or not all(isinstance(x, str) for x in used) or len(set(used)) != len(used) or not used:
            raise RegistryError("Simulation capture identities are required")
        test = {item["capture_id"]: item for item in training["split"]["test"]["sessions"]}
        if set(used) != set(test):
            raise RegistryError("Promotion simulation must cover the complete untouched test partition", failure_codes=["HOLDOUT_MISMATCH"])
        capture_map = {item["capture_id"]: item for item in manifest["captures"]}
        expected = {ident: capture_map[ident]["itch_sha256"] for ident in used}
        if report.get("input_hashes") != expected or report["input_sha256"] != digest(expected):
            raise RegistryError("Simulator input hashes do not match the complete locked holdout")
        dates = report.get("time_boundaries", {})
        train_end = max(item["session_date"] for item in training["split"]["validation"]["sessions"])
        if (dates.get("training_end_date") != train_end
                or dates.get("simulation_start_date") != min(test[x]["session_date"] for x in used)
                or dates.get("simulation_end_date") != max(test[x]["session_date"] for x in used)
                or dates["simulation_start_date"] <= train_end):
            raise RegistryError("Simulation chronology does not follow all selection data")
        # Legacy summaries omitted raw cost-stress sessions. They stay readable,
        # but cannot be promoted or loaded under stricter account valuation rules.
        if report["schema_version"] == 1:
            raise RegistryError("Legacy simulation evidence requires complete revalidation on an unused holdout",
                                failure_codes=["LEGACY_REPORT_REQUIRES_REVALIDATION"])
        plan = report.get("evaluation_plan")
        if not isinstance(plan, dict) or report.get("evaluation_plan_sha256") != digest(plan):
            raise RegistryError("A committed evaluation plan and exact digest are required")
        if (report.get("admission_sha256") != manifest["admission_sha256"]
                or plan.get("admission_sha256") != manifest["admission_sha256"]
                or plan.get("model_sha256") != manifest["model_sha256"]
                or plan.get("engine_sha256") != report["engine_sha256"]
                or plan.get("policy_sha256") != manifest["policy_sha256"]
                or plan.get("dataset_sha256") != manifest["dataset_sha256"] or plan.get("split") != "test"):
            raise RegistryError("Evaluation plan differs from the registered model/data/policy")
        sources = plan.get("python_source_sha256")
        if sources != research_source_fingerprints() or report.get("python_source_sha256") != sources:
            raise RegistryError("Research source fingerprints differ from the installed validated code")
        contract = evaluation_contract()
        if any(type(plan.get(key)) is not type(value) or plan[key] != value for key, value in contract.items()):
            raise RegistryError("Evaluation execution assumptions differ from the fixed research contract")
        gates, policy = manifest["gates"], manifest["policy"]
        if len(used) < gates["min_holdout_sessions"]:
            raise RegistryError("Insufficient independent holdout sessions", failure_codes=["HOLDOUT_MISMATCH"])
        costs = {name: policy[name] for name in ("fee_per_share_units", "min_fee_per_order_units", "slippage_buffer_units")}
        metrics = self._scenario(report.get("by_session"), report.get("metrics"), report=report,
                                 manifest=manifest, test=test, expected_costs=costs)
        if report.get("simulation_summary") != metrics:
            raise RegistryError("Simulation UI summary differs from verified account metrics", failure_codes=["AGGREGATE_MISMATCH"])
        baseline_rows, baselines = report.get("baseline_by_session"), report.get("baselines")
        if (not isinstance(baselines, dict) or set(baselines) != {"wait", "cost_aware_two_sided"}
                or not isinstance(baseline_rows, dict) or set(baseline_rows) != set(baselines)):
            raise RegistryError("Complete independent baseline evidence is required")
        for name in baselines:
            baseline = self._scenario(baseline_rows[name], baselines[name], report=report, manifest=manifest,
                                      test=test, expected_costs=costs)
            if name == "wait":
                zero_fields = ("fills", "filled_quantity", "net_pnl_units", "realized_pnl_units", "unrealized_pnl_units",
                               "fees_units", "rebates_units", "cash_before_fees_units", "max_drawdown_units",
                               "inventory", "open_orders_at_end", "turnover_units", "peak_abs_inventory_shares",
                               "inventory_share_seconds", "closed_inventory_episodes")
                if (any(baseline[field] != 0 for field in zero_fields)
                        or not baseline["valuation"]["net_pnl_valid"] or not baseline["valuation"]["drawdown_complete"]
                        or baseline["killed"] or any(type(row["simulation"].get("orders_submitted")) is not int
                            or row["simulation"]["orders_submitted"] != 0 for row in baseline_rows[name])):
                    raise RegistryError("WAIT baseline must preserve a reconciled flat account without submitted orders")
        codes = self._financial_codes(metrics, gates)
        stress = report.get("cost_stress")
        if not isinstance(stress, list) or len(stress) != 1:
            raise RegistryError("The complete fixed cost stress evidence is required", failure_codes=["MISSING_COST_STRESS"])
        qualifying = False
        for case in stress:
            factor = _number(case.get("multiplier"), "stress.multiplier", low=1, high=100)
            if factor != contract["cost_stress_multiplier"]:
                raise RegistryError("Cost stress multiplier differs from the fixed research contract")
            expected_costs = {name: math.ceil(value * factor) for name, value in costs.items()}
            if any(type(case.get(name)) is not int or case[name] != value for name, value in expected_costs.items()):
                raise RegistryError("Cost stress multiplier is not bound to actual configured costs")
            values = self._scenario(case.get("by_session"), case.get("metrics"), report=report,
                                    manifest=manifest, test=test, expected_costs=expected_costs)
            codes.extend(self._financial_codes(values, gates, stress=True))
            qualifying |= factor >= gates["required_stress_multiplier"]
        if not qualifying:
            codes.append("MISSING_COST_STRESS")
        latency = report.get("latency_stress")
        if not isinstance(latency, dict):
            raise RegistryError("Latency stress evidence is required")
        multiplier = _number(latency.get("multiplier"), "latency.multiplier", low=1, high=100, integer=True)
        if multiplier != contract["latency_stress_multiplier"]:
            raise RegistryError("Latency stress multiplier differs from the fixed research contract")
        values = self._scenario(latency.get("by_session"), latency.get("metrics"), report=report, manifest=manifest,
                                test=test, expected_costs=costs, latency_multiplier=multiplier)
        codes.extend(self._financial_codes(values, gates, stress=True))
        if codes:
            raise RegistryError("Simulation admission rejected: " + ", ".join(dict.fromkeys(codes)), failure_codes=codes)

    def validate_simulation(self, version, simulation_report):
        report = read_json(simulation_report) if isinstance(simulation_report, (str, Path)) else simulation_report
        with self._lock():
            try:
                self._validate_simulation(version, report)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                codes = getattr(exc, "failure_codes", ("INVALID_EVIDENCE",))
                self._record_attempt(version, "simulation", report, False, failure_codes=codes)
                raise RegistryError(str(exc), failure_codes=codes) from exc
            self._record_attempt(version, "simulation", report, True)
            return self._record(version, "simulation_validated", report)

    def _operator(self, path, version, *, action):
        descriptor = _open(path)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 65536:
                raise RegistryError("Operator approval must be a private, operator-owned bounded file")
            approval = _decode(stream.read(65537))
        manifest = self._manifest(version)
        evaluation, _ = self._event(version, "evaluated")
        simulation, _ = self._event(version, "simulation_validated")
        expected = {"schema_version": 1, "action": action, "approved": True, "version": version,
                    "model_sha256": manifest["model_sha256"], "policy_sha256": manifest["policy_sha256"],
                    "gates_sha256": manifest["gates_sha256"], "data_manifest_sha256": manifest["data_manifest_sha256"],
                    "validation_report_sha256": evaluation["report_sha256"],
                    "simulation_report_sha256": simulation["report_sha256"]}
        if type(approval.get("schema_version")) is not int or approval.get("approved") is not True:
            raise RegistryError("Operator approval requires an explicit true boolean")
        for key, value in expected.items():
            if approval.get(key) != value:
                raise RegistryError("Operator approval does not bind this exact model and evidence")
        if not isinstance(approval.get("operator_id"), str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,96}", approval["operator_id"]):
            raise RegistryError("Explicit operator identity is required")
        now = time.time_ns()
        issued = _number(approval.get("issued_at_ns"), "issued_at_ns", low=1, integer=True)
        if issued > now + 1_000_000_000 or now - issued > 86_400_000_000_000:
            raise RegistryError("Operator approval is future-dated or older than one day")
        if approval.get("data_authorization_confirmed") is not True:
            raise RegistryError("Operator must explicitly confirm the source authorization")
        return approval

    def _paper_checks(self, version):
        manifest = self._manifest(version)
        if manifest["provenance_kind"] != "licensed_market" or not manifest["authorized_use_attested"]:
            raise RegistryError("Synthetic or unauthorized models can never enter broker paper")
        self._check_model(version, manifest)
        evaluation, training = self._event(version, "evaluated")
        _, simulation = self._event(version, "simulation_validated")
        self._validate_training(version, training)
        self._validate_simulation(version, simulation)
        if manifest["policy"]["allow_short"] is not False:
            raise RegistryError("Paper deployment does not permit shorting")
        if evaluation.get("policy_precommitted") is not True:
            raise RegistryError("Paper thresholds must have been frozen before training began")
        age = time.time_ns() - training["completed_at_ns"]
        if age < -1_000_000_000 or age > manifest["gates"]["max_model_age_seconds"] * 1_000_000_000:
            raise RegistryError("Model is outside its approved age limit")
        return manifest

    def approve_paper(self, version, operator_file, *, data_manifest):
        from .data import load_manifest
        with self._lock():
            manifest = self._paper_checks(version)
            data = load_manifest(data_manifest)
            if (data.provenance["kind"] != "licensed_market" or not data.provenance["authorized_use_attested"]
                    or data.manifest_sha256 != manifest["data_manifest_sha256"] or data.dataset_sha256 != manifest["dataset_sha256"]):
                raise RegistryError("Reverified licensed data does not match the candidate")
            approval = self._operator(operator_file, version, action="approve_paper")
            return self._record(version, "operator_paper_approved", approval)

    def activate(self, version, *, mode="simulation", operator_file=None, rollback=False):
        if mode not in {"simulation", "paper"}:
            raise RegistryError("Live activation is never supported")
        with self._lock():
            manifest = self._manifest(version)
            self._check_model(version, manifest)
            if mode == "paper":
                self._paper_checks(version)
            _, simulation = self._event(version, "simulation_validated")
            self._validate_simulation(version, simulation)
            path = self.root / f"active-{mode}.json"
            previous = read_json(path, limit=16384) if _present(path) else None
            if mode == "paper":
                self._event(version, "operator_paper_approved")
                if operator_file is None:
                    raise RegistryError("Paper activation requires explicit operator approval")
                approval = self._operator(operator_file, version, action="rollback" if rollback else "approve_paper")
                if rollback and (not previous or approval.get("from_version") != previous.get("version")):
                    raise RegistryError("Rollback approval must bind the currently active version")
            elif rollback and operator_file is None:
                raise RegistryError("Rollback requires an explicit operator approval file")
            if rollback and mode == "simulation":
                approval = self._operator(operator_file, version, action="rollback")
                if not previous or approval.get("from_version") != previous.get("version"):
                    raise RegistryError("Rollback source version differs from approval")
            generation = 1 if previous is None else _number(previous.get("generation"), "generation", low=1, integer=True) + 1
            pointer = {"schema_version": 1, "mode": mode, "version": version,
                       "model_sha256": manifest["model_sha256"], "policy_sha256": manifest["policy_sha256"],
                       "gates_sha256": manifest["gates_sha256"], "generation": generation,
                       "previous_version": previous.get("version") if previous else None,
                       "activated_at_ns": time.time_ns(), "rollback": rollback}
            _write(self.root / "history" / f"{mode}-{generation:012d}.json", pointer)
            _write(path, pointer, replace=True)
            return pointer

    def rollback(self, mode, target_version, operator_file):
        return self.activate(target_version, mode=mode, operator_file=operator_file, rollback=True)


def load_approved_model(store, mode="paper") -> ApprovedModel:
    if mode not in {"paper", "simulation"}:
        raise RegistryError("Live model loading is always rejected")
    registry = Registry(store, create=False)
    pointer = read_json(registry.root / f"active-{mode}.json", limit=16384)
    if pointer.get("schema_version") != 1 or pointer.get("mode") != mode:
        raise RegistryError("Invalid active pointer")
    version = pointer.get("version")
    manifest = registry._manifest(version)
    registry._check_model(version, manifest)
    if (pointer.get("model_sha256") != manifest["model_sha256"]
            or pointer.get("policy_sha256") != manifest["policy_sha256"]
            or pointer.get("gates_sha256") != manifest["gates_sha256"]):
        raise RegistryError("Active pointer hash mismatch")
    _, report = registry._event(version, "simulation_validated")
    registry._validate_simulation(version, report)
    if mode == "paper":
        registry._paper_checks(version)
        _, approval = registry._event(version, "operator_paper_approved")
        if approval.get("approved") is not True or approval.get("action") != "approve_paper":
            raise RegistryError("Explicit paper approval is missing")
    return ApprovedModel(registry._version(version) / "model.json", version, manifest["model_sha256"], manifest)


def _attempt_summary(registry, version, kind):
    directory = registry.root / "events" / version
    if _present(directory):
        _trusted_directory(directory)
    path = registry.root / "events" / version / f"latest-{kind}-attempt.json"
    if not _present(path):
        return None, None, []
    attempt = read_json(path, limit=16384)
    manifest = registry._manifest(version)
    identity = attempt.get("report_sha256")
    if (attempt.get("version") != version or attempt.get("model_sha256") != manifest["model_sha256"]
            or type(attempt.get("accepted")) is not bool or not HASH_RE.fullmatch(str(identity))):
        raise RegistryError("Invalid evaluation attempt")
    codes = attempt.get("failure_codes", [] if attempt["accepted"] else ["LEGACY_FAILURE_UNCLASSIFIED"])
    if not isinstance(codes, list) or any(code not in FAILURE_CODES for code in codes) or len(set(codes)) != len(codes):
        raise RegistryError("Invalid evaluation failure codes")
    if attempt["accepted"] and codes:
        raise RegistryError("Accepted evidence cannot carry rejection codes")
    report = read_json(registry.root / "evidence" / f"{identity}.json")
    if digest(report) != identity:
        raise RegistryError("Evaluation attempt hash mismatch")
    if report.get("model_version") != version or report.get("model_sha256") != manifest["model_sha256"]:
        return None, False, ["INVALID_EVIDENCE"]  # Do not display a foreign model's rejected evidence.
    if kind == "training":
        summary = {"split": {}, "metrics": {}, "selected_alpha": report.get("selected_alpha")}
        metric_fields = {"mse_bps2", "mae_bps", "rmse_bps", "correlation", "direction_accuracy", "samples"}
        for region in ("train", "validation", "test"):
            group = report.get("split", {}).get(region, {})
            sessions = group.get("sessions")
            if not isinstance(sessions, list):
                return None, attempt["accepted"], codes
            summary["split"][region] = {"sessions": len(sessions), "samples": group.get("samples")}
            summary["metrics"][region] = {}
            for estimator in ("ridge", "zero", "microprice"):
                metric = report.get("metrics", {}).get(region, {}).get(estimator, {})
                if not metric_fields.issubset(metric):
                    return None, attempt["accepted"], codes
                summary["metrics"][region][estimator] = {key: metric[key] for key in metric_fields}
        return summary, attempt["accepted"], codes
    fields = {"events", "fills", "fill_count", "filled_quantity", "realized_pnl_units", "unrealized_pnl_units",
              "fees_units", "rebates_units", "net_pnl_units", "max_drawdown_units", "inventory",
              "open_orders_at_end", "pnl_mark_valid", "killed", "open_exposure", "valuation",
              "cash_before_fees_units", "residual_inventory_by_session"}
    values = report.get("metrics", {})
    return {key: values[key] for key in fields if key in values}, attempt["accepted"], codes


def read_status(store):
    """Bounded, allowlisted projection: never expose paths, source strings or weights."""
    empty = {"schema_version": 1, "available": False, "state": "not_configured", "live_enabled": False,
             "active": {"simulation": None, "paper": None}, "models": [], "retrain": None}
    if not store:
        return empty
    token = _READ_BUDGET.set([16 * 1024 * 1024, 256])
    try:
        registry = Registry(store, create=False)
        index_path = registry.root / "index.json"
        index = read_json(index_path, limit=128 * 1024) if _present(index_path) else {"schema_version": 1, "models": []}
        if index.get("schema_version") != 1 or not isinstance(index.get("models"), list) or len(index["models"]) > 100:
            raise RegistryError("Invalid model index")
        answer = {**empty, "available": True, "state": "ready", "active": dict(empty["active"]), "models": []}
        for mode in ("simulation", "paper"):
            pointer_path = registry.root / f"active-{mode}.json"
            if _present(pointer_path):
                pointer = read_json(pointer_path, limit=16384)
                manifest = registry._manifest(pointer.get("version"))
                if (pointer.get("schema_version") != 1 or pointer.get("mode") != mode
                        or pointer.get("model_sha256") != manifest["model_sha256"]
                        or pointer.get("policy_sha256") != manifest["policy_sha256"]
                        or pointer.get("gates_sha256") != manifest["gates_sha256"]):
                    raise RegistryError("Invalid active pointer")
                answer["active"][mode] = manifest["version"]
        for entry in index["models"][:50]:
            version, stage = entry.get("version"), entry.get("stage")
            if stage not in STAGES:
                raise RegistryError("Unknown model stage")
            manifest = registry._manifest(version)
            registry._check_model(version, manifest)
            if stage != "candidate":
                registry._event(version, stage)
            training, evaluated, evaluation_codes = _attempt_summary(registry, version, "training")
            simulation, simulated, simulation_codes = _attempt_summary(registry, version, "simulation")
            answer["models"].append({key: manifest[key] for key in
                                    ("version", "provenance_kind", "registered_at_ns", "model_sha256", "policy_sha256")}
                                   | {"stage": stage, "training_summary": training, "simulation_summary": simulation,
                                      "evaluation_passed": evaluated, "simulation_passed": simulated,
                                      "evaluation_failure_codes": evaluation_codes, "simulation_failure_codes": simulation_codes})
        retrain_path = registry.root / "retrain" / "latest.json"
        if _present(retrain_path):
            record = read_json(retrain_path, limit=16384)
            allowed_states = {"idle", "requested", "running", "completed", "failed", "skipped", "insufficient_data", "blocked"}
            if record.get("state") not in allowed_states:
                raise RegistryError("Unknown retrain state")
            safe_reasons = {"NOT_DUE", "PERIODIC", "FEATURE_DRIFT", "INSUFFICIENT_WINDOW", "NO_NEW_AUTHORIZED_DATA",
                            "DATA_ALREADY_ATTEMPTED", "TRAINING_FAILED", "CREATED_CANDIDATE", "DRY_RUN", "NO_MODEL", "DATA_INVALID"}
            reason = record.get("reason")
            answer["retrain"] = {"state": record["state"], "reason": reason if reason in safe_reasons else "OPERATOR_REVIEW_REQUIRED",
                                  "updated_at_ns": record["updated_at_ns"]}
        if not answer["models"]:
            answer["state"] = "empty"
        return answer
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return {**empty, "state": "invalid"}
    finally:
        _READ_BUDGET.reset(token)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install-policy")
    install.add_argument("--file", required=True)
    register = sub.add_parser("register")
    register.add_argument("--model", required=True)
    register.add_argument("--manifest", required=True)
    register.add_argument("--admission-sha256", required=True)
    for action in ("evaluate", "validate-simulation"):
        item = sub.add_parser(action)
        item.add_argument("--version", required=True)
        item.add_argument("--report", required=True)
    approval = sub.add_parser("approve-paper")
    approval.add_argument("--version", required=True)
    approval.add_argument("--operator-file", required=True)
    approval.add_argument("--manifest", required=True)
    for action in ("activate", "rollback"):
        item = sub.add_parser(action)
        item.add_argument("--version", required=True)
        item.add_argument("--mode", choices=("simulation", "paper"), required=True)
        item.add_argument("--operator-file", required=action == "rollback")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    try:
        registry = Registry(args.store, create=args.command != "status")
        if args.command == "install-policy":
            result = {"admission_sha256": registry.install_policy(args.file)}
        elif args.command == "register":
            result = {"version": registry.register(args.model, admission_sha256=args.admission_sha256, data_manifest=args.manifest)}
        elif args.command == "evaluate":
            result = registry.evaluate(args.version, args.report)
        elif args.command == "validate-simulation":
            result = registry.validate_simulation(args.version, args.report)
        elif args.command == "approve-paper":
            result = registry.approve_paper(args.version, args.operator_file, data_manifest=args.manifest)
        elif args.command in {"activate", "rollback"}:
            result = registry.activate(args.version, mode=args.mode, operator_file=args.operator_file, rollback=args.command == "rollback")
        else:
            result = read_status(args.store)
        print(json.dumps({"success": True, "data": result}, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({"success": False, "code": "REGISTRY_REJECTED", "message": str(exc),
                          "failure_codes": list(getattr(exc, "failure_codes", ("INVALID_EVIDENCE",)))}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
