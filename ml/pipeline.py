"""One resumable offline research job; never activates a model or calls a broker.

The operator-owned run directory is a local trust boundary. A completed stage is
accepted only after rechecking its artifacts; status files alone are not evidence.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from .data import load_manifest
from .features import FeatureConfig
from .model import LinearModel
from .registry import Registry, _mkdir, _open, _trusted_directory, _write, digest, read_json
from .reservations import ReservationConflict, reserve
from .training import TrainingConfig

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("policy", "training", "registration", "prediction_gate", "backtest", "simulation_gate")
SOURCES = ("pipeline.py", "train.py", "training.py", "features.py", "model.py", "data.py",
           "runtime.py", "backtest.py", "registry.py", "valuation.py", "reservations.py", "retrain.py")
TRAINING_FILES = ("model.json", "report.json", "config.json", "data-manifest.json", "train.log")


class PipelineError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def default_config():
    return {"schema_version": 1, "training": {**asdict(TrainingConfig()), "alphas": list(TrainingConfig().alphas)},
            "features": asdict(FeatureConfig()), "timeout_seconds": 1800}


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "training", "features", "timeout_seconds"} or value["schema_version"] != 1:
        raise PipelineError("CONFIG_INVALID", "Pipeline configuration requires the complete supported schema")
    training = value["training"]
    if not isinstance(training, dict) or set(training) != set(asdict(TrainingConfig())) or not isinstance(training.get("alphas"), list):
        raise PipelineError("CONFIG_INVALID", "Every frozen training parameter is required")
    validated = TrainingConfig(**{**training, "alphas": tuple(training["alphas"])}).validate()
    features = FeatureConfig.from_dict(value["features"])
    timeout = value["timeout_seconds"]
    if type(timeout) is not int or not 10 <= timeout <= 7200:
        raise PipelineError("CONFIG_INVALID", "Stage timeout must be between 10 and 7200 seconds")
    return {"schema_version": 1, "training": {**asdict(validated), "alphas": list(validated.alphas)},
            "features": asdict(features), "timeout_seconds": timeout}


def _absolute(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise PipelineError("UNSAFE_PATH", "Parent path traversal is forbidden")
    return path


def _safe_file_hash(path, *, maximum=512 * 1024 * 1024):
    descriptor = _open(path)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > maximum:
            raise PipelineError("ARTIFACT_TOO_LARGE", "Stage artifact exceeds the supported file size")
        result = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _inventory(directory):
    directory = _absolute(directory)
    _trusted_directory(directory)
    result, total = {}, 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs:
            _trusted_directory(Path(parent) / name)
        for name in files:
            path = Path(parent) / name
            result[path.relative_to(directory).as_posix()] = _safe_file_hash(path)
            total += path.stat().st_size
            if len(result) > 20000 or total > 4 * 1024**3:
                raise PipelineError("ARTIFACT_TOO_LARGE", "Stage artifacts exceed the bounded run size")
    return result


@contextmanager
def _run_lock(directory, resume):
    directory = _absolute(directory)
    if directory.exists() or directory.is_symlink():
        if not resume:
            raise PipelineError("RUN_EXISTS", "Run already exists; use --resume with the exact original request")
    else:
        if resume:
            raise PipelineError("RUN_MISSING", "Cannot resume a missing run")
        _mkdir(directory)
    _trusted_directory(directory)
    descriptor = os.open(directory / ".pipeline.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022:
            raise PipelineError("UNSAFE_PATH", "Unsafe pipeline lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineError("PIPELINE_BUSY", "Another process owns this run; no duplicate job was started") from exc
        yield directory
    finally:
        os.close(descriptor)


def _request(manifest_path, store, admission_path, config, binary):
    dataset = load_manifest(manifest_path)
    admission = read_json(admission_path)
    from .registry import validate_admission
    validate_admission(admission)
    binary = _absolute(binary)
    binary_hash = _safe_file_hash(binary)
    if not os.access(binary, os.X_OK):
        raise PipelineError("ENGINE_UNAVAILABLE", "Configured C++ replay binary is not executable")
    config = validate_config(config)
    # An absent optional source is part of the fingerprint too: adding a new
    # valuation implementation must not silently change a resumed experiment.
    source = {name: _safe_file_hash(ROOT / "ml" / name) if (ROOT / "ml" / name).exists() else None for name in SOURCES}
    try:
        dependencies = {name: package_version(name) for name in ("numpy", "scipy", "scikit-learn", "joblib", "threadpoolctl")}
    except PackageNotFoundError as exc:
        raise PipelineError("DEPENDENCIES_UNAVAILABLE", "Install requirements-ml.txt in the offline training environment") from exc
    request = {"schema_version": 1, "manifest_path": str(dataset.path), "store_path": str(_absolute(store)),
               "dataset_sha256": dataset.dataset_sha256, "manifest_sha256": dataset.manifest_sha256,
               "provenance_kind": dataset.provenance["kind"], "admission_path": str(_absolute(admission_path)),
               "admission_sha256": digest(admission), "config": config, "engine_path": str(binary),
               "engine_sha256": binary_hash, "source_sha256": source,
               "python_version": list(sys.version_info[:3]),
               "dependencies": dependencies,
               "split": "test", "automatic_promotion": False, "execution_authorized": False}
    return request, admission


def _training_command(request, output):
    training, feature = request["config"]["training"], request["config"]["features"]
    command = [sys.executable, "-m", "ml.train", "--manifest", request["manifest_path"], "--output", str(output),
               "--exporter", request["engine_path"]]
    for name in ("horizon_ns", "max_label_delay_ns", "seed", "min_samples_per_session", "max_abs_prediction_bps"):
        command += ["--" + name.replace("_", "-"), str(training[name])]
    for name in ("window_events", "max_gap_ns", "regular_open_ns", "regular_close_ns"):
        command += ["--" + name.replace("_", "-"), str(feature[name])]
    return command + ["--alphas", *map(str, training["alphas"])]


def _execute(command, stage_dir, timeout):
    _mkdir(stage_dir)
    _write(stage_dir / "invocation.json", {"schema_version": 1, "command": command,
                                           "timeout_seconds": timeout, "started_at_ns": time.time_ns()})
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PYTHONPATH": str(ROOT),
                   "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    started, timed_out = time.time_ns(), False
    with (stage_dir / "stdout.log").open("xb") as stdout, (stage_dir / "stderr.log").open("xb") as stderr:
        try:
            process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=stdout, stderr=stderr, start_new_session=True)
        except OSError as exc:
            _write(stage_dir / "exit.json", {"schema_version": 1, "started_at_ns": started,
                   "completed_at_ns": time.time_ns(), "returncode": None, "timed_out": False,
                   "spawned": False, "error_type": type(exc).__name__})
            raise
        process_identity = _process_identity(process.pid)
        _write(stage_dir / "process.json", {"schema_version": 1, "pid": process.pid, **process_identity})
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                returncode = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                returncode = process.wait()
        for stream in (stdout, stderr):
            stream.flush()
            os.fsync(stream.fileno())
    result = {"schema_version": 1, "started_at_ns": started, "completed_at_ns": time.time_ns(),
              "returncode": returncode, "timed_out": timed_out, "spawned": True}
    _write(stage_dir / "exit.json", result)
    return result


def _process_identity(pid):
    try:
        # /proc/<pid>/stat field 2 may contain spaces and parentheses.
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return {"start_ticks": int(fields[19]), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "process_state": fields[0]}
    except FileNotFoundError:
        return {"start_ticks": None, "boot_id": None, "process_state": "exited"}


def _assert_child_stopped(stage_dir):
    path = stage_dir / "process.json"
    if not path.exists() and not path.is_symlink():
        return
    saved = read_json(path, limit=16384)
    if type(saved.get("pid")) is not int or not 0 < saved["pid"] < 2**31:
        raise PipelineError("ARTIFACT_MISMATCH", "Invalid interrupted process identity")
    current = _process_identity(saved["pid"])
    if (current["start_ticks"] is not None and current["start_ticks"] == saved.get("start_ticks")
            and current["boot_id"] == saved.get("boot_id") and current["process_state"] != "Z"):
        raise PipelineError("STAGE_STILL_RUNNING", "The interrupted stage still owns a live child process; wait for it to finish before resuming")


def _verify_exit(name, exit_status, outcome):
    expected = 2 if name in ("prediction_gate", "simulation_gate") and outcome["state"] == "rejected" else 0
    if exit_status.get("timed_out") is not False or type(exit_status.get("returncode")) is not int or exit_status["returncode"] != expected:
        raise PipelineError("STAGE_FAILED", "Process exit does not match a successful stage or a deliberate registry rejection")


def _verify_training(run, request):
    directory = run / "training"
    if any(not (directory / name).is_file() or (directory / name).is_symlink() for name in TRAINING_FILES):
        raise PipelineError("INCOMPLETE_TRAINING", "Training output is incomplete; inspect logs, do not delete holdout locks or retrain automatically")
    model = LinearModel.load(directory / "model.json")
    report, config = read_json(directory / "report.json"), read_json(directory / "config.json")
    metadata = model.training_metadata
    expected_config = request["config"]
    if (metadata.get("dataset_sha256") != request["dataset_sha256"]
            or metadata.get("data_manifest_sha256") != request["manifest_sha256"]
            or metadata.get("exporter_sha256") != request["engine_sha256"]
            or metadata.get("market_exports_verified") is not True
            or metadata.get("config") != config or config.get("training") != expected_config["training"]
            or config.get("features") != expected_config["features"]
            or report.get("model_sha256") != _safe_file_hash(directory / "model.json")
            or report.get("model_version") != model.model_version
            or report.get("dataset_sha256") != request["dataset_sha256"]
            or report.get("data_manifest_sha256") != request["manifest_sha256"]
            or report.get("split") != metadata.get("split")
            or read_json(directory / "data-manifest.json") != read_json(request["manifest_path"])):
        raise PipelineError("ARTIFACT_MISMATCH", "Training output differs from its frozen request")
    log_path = directory / "train.log"
    _safe_file_hash(log_path, maximum=8 * 1024 * 1024)
    lines = log_path.read_text().splitlines()
    completed = json.loads(lines[-1]) if lines else {}
    if completed.get("event") != "training_complete" or completed.get("model_version") != model.model_version:
        raise PipelineError("INCOMPLETE_TRAINING", "A complete training log is required before recovery")
    return model, report


def _verify_backtest(run, request, model):
    directory = run / "backtest"
    if not (directory / "report.json").is_file() or (directory / "report.json").is_symlink():
        raise PipelineError("INCOMPLETE_BACKTEST", "Backtest is incomplete; preserve all evidence and consumed test locks")
    report = read_json(directory / "report.json")
    plan = read_json(directory / "evaluation-plan.json")
    test = model.training_metadata["split"]["test"]["sessions"]
    dataset = load_manifest(request["manifest_path"])
    wanted = [entry["capture_id"] for entry in test]
    input_hashes = {capture.capture_id: capture.itch_sha256 for capture in dataset.captures if capture.capture_id in wanted}
    if (report.get("schema_version") != 2 or report.get("report_type") != "simulation_validation"
            or report.get("split") != "test" or report.get("model_version") != model.model_version
            or report.get("model_sha256") != _safe_file_hash(run / "training/model.json")
            or report.get("engine_sha256") != request["engine_sha256"]
            or report.get("admission_sha256") != request["admission_sha256"]
            or report.get("capture_ids") != wanted or report.get("input_hashes") != input_hashes
            or report.get("input_sha256") != digest(input_hashes) or report.get("execution_authorized") is not False
            or plan.get("dataset_sha256") != request["dataset_sha256"]
            or plan.get("model_sha256") != report.get("model_sha256")
            or plan.get("engine_sha256") != report.get("engine_sha256")
            or plan.get("admission_sha256") != request["admission_sha256"]
            or report.get("python_source_sha256") != plan.get("python_source_sha256")):
        raise PipelineError("ARTIFACT_MISMATCH", "Backtest output differs from its frozen model, input or test interval")
    hashes = report.get("python_source_sha256")
    if not isinstance(hashes, dict) or not {"backtest.py", "features.py", "runtime.py", "model.py", "data.py", "valuation.py"}.issubset(hashes):
        raise PipelineError("ARTIFACT_MISMATCH", "Backtest source identity is missing")
    if any(request["source_sha256"].get(name) != value for name, value in hashes.items()):
        raise PipelineError("ARTIFACT_MISMATCH", "Backtest was produced by different source code")
    for capture_id in wanted:
        binding = read_json(directory / f"{capture_id}-binding.json")
        if (binding.get("capture_id") != capture_id or binding.get("itch_sha256") != input_hashes[capture_id]
                or binding.get("model_version") != model.model_version or binding.get("policy_sha256") != report.get("policy_sha256")):
            raise PipelineError("ARTIFACT_MISMATCH", "Signal file binding does not match evaluated model/input")
        for name in ("model", "baseline", "wait"):
            if binding.get("signals", {}).get(name) != _safe_file_hash(directory / f"{capture_id}-{name}.csv"):
                raise PipelineError("ARTIFACT_MISMATCH", "Signal file hash differs from its immutable binding")
        for name in ("model", "cost_aware_two_sided", "wait", "double_cost", "fivefold_latency"):
            read_json(directory / f"{capture_id}-{name}-engine.json")
    return report


def _gate_result(registry, version, kind, report, stage_dir):
    path = registry.root / "events" / version / f"latest-{kind}-attempt.json"
    attempt = read_json(path, limit=16384)
    manifest = registry._manifest(version)
    if (attempt.get("version") != version or attempt.get("kind") != kind
            or attempt.get("model_sha256") != manifest["model_sha256"]
            or attempt.get("report_sha256") != digest(report) or type(attempt.get("accepted")) is not bool):
        raise PipelineError("GATE_EVIDENCE_MISMATCH", "Registry did not record this exact evaluation attempt")
    invocation = read_json(stage_dir / "invocation.json", limit=16384)
    before = read_json(stage_dir / "gate-before.json", limit=16384)
    prior_sha = before.get("previous_attempt_sha256")
    if (before.get("schema_version") != 1 or before.get("version") != version or before.get("kind") != kind
            or prior_sha is not None and (not isinstance(prior_sha, str) or len(prior_sha) != 64
                                         or any(char not in "0123456789abcdef" for char in prior_sha))
            or prior_sha == _safe_file_hash(path)):
        raise PipelineError("GATE_EVIDENCE_MISMATCH", "The registry attempt did not change after this gate was scheduled")
    started, updated = invocation.get("started_at_ns"), attempt.get("updated_at_ns")
    captured = before.get("captured_at_ns")
    if (type(started) is not int or type(updated) is not int or type(captured) is not int
            or not 0 < captured <= started <= updated):
        raise PipelineError("GATE_EVIDENCE_MISMATCH", "An older registry attempt cannot complete this invocation")
    immutable = registry.root / "events" / version / f"{kind}-attempt-{digest(attempt)}.json"
    if read_json(immutable, limit=16384) != attempt:
        raise PipelineError("GATE_EVIDENCE_MISMATCH", "Latest gate pointer lacks its immutable attempt record")
    exit_path = stage_dir / "exit.json"
    if exit_path.exists() or exit_path.is_symlink():
        completed = read_json(exit_path, limit=16384).get("completed_at_ns")
        if type(completed) is not int or not updated <= completed:
            raise PipelineError("GATE_EVIDENCE_MISMATCH", "Gate evidence was created after this subprocess completed")
    evidence = read_json(registry.root / "evidence" / f"{attempt['report_sha256']}.json")
    if digest(evidence) != digest(report):
        raise PipelineError("GATE_EVIDENCE_MISMATCH", "Registry evidence has changed")
    if attempt["accepted"]:
        registry._event(version, "evaluated" if kind == "training" else "simulation_validated")
    codes = attempt.get("failure_codes", [])
    if not isinstance(codes, list) or any(not isinstance(value, str) or len(value) > 96 for value in codes):
        raise PipelineError("GATE_EVIDENCE_MISMATCH", "Invalid gate failure codes")
    return {"state": "succeeded" if attempt["accepted"] else "rejected", "accepted": attempt["accepted"],
            "report_sha256": digest(report), "failure_codes": codes,
            "registry_attempt_sha256": _safe_file_hash(path)}


def _verify_stage(name, run, request):
    registry = Registry(request["store_path"], create=False)
    if name == "policy":
        stored = registry._policy(request["admission_sha256"])
        if digest(stored["admission"]) != request["admission_sha256"]:
            raise PipelineError("ARTIFACT_MISMATCH", "Predeclared policy is not installed")
        return {"state": "succeeded", "admission_sha256": request["admission_sha256"]}
    model, training = _verify_training(run, request)
    if name == "training":
        policy = registry._policy(request["admission_sha256"])
        if policy["registered_at_ns"] > training["trained_at_ns"]:
            raise PipelineError("POLICY_NOT_PRECOMMITTED", "Policy must be installed before training begins")
        return {"state": "succeeded", "model_version": model.model_version}
    if name in ("registration", "prediction_gate", "simulation_gate"):
        registered = registry._manifest(model.model_version)
        registry._check_model(model.model_version, registered)
        if (registered["model_sha256"] != _safe_file_hash(run / "training/model.json")
                or registered["admission_sha256"] != request["admission_sha256"]
                or registered["dataset_sha256"] != request["dataset_sha256"]):
            raise PipelineError("ARTIFACT_MISMATCH", "Registry candidate differs from this run")
        if name == "registration":
            return {"state": "succeeded", "model_version": model.model_version}
        if name == "prediction_gate":
            return _gate_result(registry, model.model_version, "training", training, run / "stages" / name)
    report = _verify_backtest(run, request, model)
    if name == "backtest":
        return {"state": "succeeded", "model_version": model.model_version}
    return _gate_result(registry, model.model_version, "simulation", report, run / "stages" / name)


def _stage_inventory(name, run):
    result = {"stage/" + key: value for key, value in _inventory(run / "stages" / name).items() if key != "receipt.json"}
    directory = run / name if name in ("training", "backtest") else None
    if directory is not None:
        result.update({"output/" + key: value for key, value in _inventory(directory).items()})
    return result


def _command(name, run, request):
    prefix = [sys.executable, "-m", "ml.registry", "--store", request["store_path"]]
    if name == "policy":
        return prefix + ["install-policy", "--file", str(run / "admission.json")]
    if name == "training":
        return _training_command(request, run / "training")
    model = LinearModel.load(run / "training/model.json")
    if name == "registration":
        return prefix + ["register", "--model", str(run / "training/model.json"), "--manifest", request["manifest_path"],
                         "--admission-sha256", request["admission_sha256"]]
    if name == "prediction_gate":
        return prefix + ["evaluate", "--version", model.model_version, "--report", str(run / "training/report.json")]
    if name == "backtest":
        return [sys.executable, "-m", "ml.backtest", "--manifest", request["manifest_path"],
                "--training-run", str(run / "training"), "--output", str(run / "backtest"),
                "--binary", request["engine_path"], "--admission", str(run / "admission.json"), "--split", "test"]
    return prefix + ["validate-simulation", "--version", model.model_version, "--report", str(run / "backtest/report.json")]


def _prepare_gate(name, run, request):
    if name not in ("prediction_gate", "simulation_gate"):
        return
    registry = Registry(request["store_path"], create=False)
    model = LinearModel.load(run / "training/model.json")
    kind = "training" if name == "prediction_gate" else "simulation"
    with registry._lock():
        path = registry.root / "events" / model.model_version / f"latest-{kind}-attempt.json"
        previous = _safe_file_hash(path) if path.exists() or path.is_symlink() else None
        _write(run / "stages" / name / "gate-before.json", {"schema_version": 1,
               "version": model.model_version, "kind": kind, "previous_attempt_sha256": previous,
               "captured_at_ns": time.time_ns()})


def run_pipeline(manifest, run_dir, store, admission, *, config=None, binary=None, resume=False):
    config = validate_config(config or default_config())
    request, admission_document = _request(manifest, store, admission, config, binary or ROOT / "build/trading-ml-replay")
    request["run_path"] = str(_absolute(run_dir))
    request_sha = digest(request)
    with _run_lock(run_dir, resume) as run:
        request_file = run / "request.json"
        if resume:
            saved = read_json(request_file)
            if saved != {"request_sha256": request_sha, "request": request}:
                raise PipelineError("REQUEST_CHANGED", "Data, policy, configuration, engine or source changed; a consumed test cannot be rerun")
            if read_json(run / "admission.json") != admission_document:
                raise PipelineError("ARTIFACT_MISMATCH", "Frozen admission file changed")
        else:
            _write(request_file, {"request_sha256": request_sha, "request": request})
            _write(run / "admission.json", admission_document)
            _mkdir(run / "stages")
        state_path = run / "status.json"
        state = {"schema_version": 1, "request_sha256": request_sha, "state": "running", "stages": {},
                 "automatic_promotion": False, "execution_authorized": False, "updated_at_ns": time.time_ns()}
        if resume and state_path.exists():
            state = read_json(state_path)
            if state.get("schema_version") != 1 or state.get("request_sha256") != request_sha or not isinstance(state.get("stages"), dict):
                raise PipelineError("ARTIFACT_MISMATCH", "Invalid pipeline status")
        def save():
            state["updated_at_ns"] = time.time_ns()
            _write(state_path, state, replace=True)
        try:
            registry = Registry(request["store_path"])
            with registry._lock():
                try:
                    reservation = reserve(registry, load_manifest(request["manifest_path"]), owner="pipeline",
                                          request_sha256=request_sha, allow_existing=resume)
                except ReservationConflict as exc:
                    raise PipelineError("HOLDOUT_ALREADY_RESERVED", str(exc)) from exc
                reservation_path = run / "reservation.json"
                if reservation_path.exists() or reservation_path.is_symlink():
                    if read_json(reservation_path) != reservation:
                        raise PipelineError("ARTIFACT_MISMATCH", "Run reservation differs from the retained registry ledger")
                else:
                    _write(reservation_path, reservation)
            for name in STAGES:
                current, _ = _request(manifest, store, admission, config, request["engine_path"])
                current["run_path"] = str(run)
                if current != request:
                    raise PipelineError("REQUEST_CHANGED", "Inputs changed during this job; no further stage may run")
                stage_dir = run / "stages" / name
                receipt_path = stage_dir / "receipt.json"
                state["current_stage"] = name
                if receipt_path.exists() or receipt_path.is_symlink():
                    receipt = read_json(receipt_path)
                    outcome = _verify_stage(name, run, request)
                    if (receipt.get("request_sha256") != request_sha or receipt.get("artifacts") != _stage_inventory(name, run)
                            or receipt.get("outcome") != outcome):
                        raise PipelineError("ARTIFACT_MISMATCH", "A previously completed stage or its registry evidence changed")
                    state["stages"][name] = {**outcome, "recovered": bool(receipt.get("recovered")),
                                             "recovered_unknown_exit": bool(receipt.get("recovered_unknown_exit"))}
                    continue
                if stage_dir.exists() or stage_dir.is_symlink():
                    # No retry: only complete artifacts from this exact interrupted
                    # invocation may be accepted. Partial output remains blocked.
                    invocation = read_json(stage_dir / "invocation.json")
                    if invocation.get("command") != _command(name, run, request):
                        raise PipelineError("ARTIFACT_MISMATCH", "Interrupted stage command differs from the frozen request")
                    _assert_child_stopped(stage_dir)
                    try:
                        outcome = _verify_stage(name, run, request)
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        raise PipelineError("INCOMPLETE_STAGE", "Interrupted stage lacks complete verified artifacts; preserve evidence and investigate without deleting holdout locks") from exc
                    recovered = True
                    exit_path = stage_dir / "exit.json"
                    recovered_unknown_exit = not exit_path.exists() and not exit_path.is_symlink()
                    if not recovered_unknown_exit:
                        _verify_exit(name, read_json(exit_path), outcome)
                else:
                    state["state"] = "running"
                    state["stages"][name] = {"state": "running"}
                    save()
                    _prepare_gate(name, run, request)
                    exit_status = _execute(_command(name, run, request), stage_dir, config["timeout_seconds"])
                    if exit_status["timed_out"]:
                        raise PipelineError("STAGE_TIMEOUT", "Stage timed out; no automatic retry or holdout reset is permitted")
                    if exit_status["returncode"] and name not in ("prediction_gate", "simulation_gate"):
                        raise PipelineError("STAGE_FAILED", "Stage failed; inspect recorded stdout, stderr and exit.json")
                    try:
                        outcome = _verify_stage(name, run, request)
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        raise PipelineError("STAGE_FAILED", "Stage did not produce complete bound evidence; inspect logs") from exc
                    _verify_exit(name, exit_status, outcome)
                    recovered = False
                    recovered_unknown_exit = False
                receipt = {"schema_version": 1, "request_sha256": request_sha, "stage": name,
                           "outcome": outcome, "recovered": recovered, "recovered_unknown_exit": recovered_unknown_exit,
                           "artifacts": _stage_inventory(name, run),
                           "sealed_at_ns": time.time_ns()}
                _write(receipt_path, receipt)
                state["stages"][name] = {**outcome, "recovered": recovered, "recovered_unknown_exit": recovered_unknown_exit}
                save()
            state.update(state="completed", current_stage=None,
                         model_version=LinearModel.load(run / "training/model.json").model_version,
                         admission_passed=all(state["stages"][key]["state"] == "succeeded" for key in ("prediction_gate", "simulation_gate")),
                         automatic_promotion=False, execution_authorized=False)
            state.pop("failure", None)
            save()
            return state
        except (ValueError, OSError, KeyError, TypeError) as exc:
            code = exc.code if isinstance(exc, PipelineError) else "PIPELINE_FAILED"
            state.update(state="blocked" if code in {"REQUEST_CHANGED", "ARTIFACT_MISMATCH", "INCOMPLETE_STAGE", "STAGE_STILL_RUNNING", "HOLDOUT_ALREADY_RESERVED"} else "failed",
                         failure={"code": code, "message": str(exc)}, automatic_promotion=False, execution_authorized=False)
            if state.get("current_stage") in STAGES:
                state["stages"][state["current_stage"]] = {"state": state["state"], "failure_code": code}
            save()
            if isinstance(exc, PipelineError):
                raise
            raise PipelineError(code, str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--admission", required=True)
    parser.add_argument("--config")
    parser.add_argument("--binary", default=str(ROOT / "build/trading-ml-replay"))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(args.manifest, args.run_dir, args.store, args.admission,
                              config=read_json(args.config) if args.config else None, binary=args.binary, resume=args.resume)
        print(json.dumps(result, allow_nan=False))
        # A completed research job may legitimately reject every candidate.
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"state": "blocked", "code": getattr(exc, "code", "PIPELINE_FAILED"),
                          "message": str(exc), "execution_authorized": False}), file=sys.stderr)
        return 4 if getattr(exc, "code", "") == "PIPELINE_BUSY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
