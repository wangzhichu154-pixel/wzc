"""Chronological Ridge baseline with train-only preprocessing and a locked test."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import time

from .data import build_labeled_sessions, load_manifest, sha256_file, verify_market_exports
from .features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FeatureConfig
from .model import LinearModel, artifact_version, canonical_bytes, write_json


@dataclass(frozen=True)
class TrainingConfig:
    horizon_ns: int = 500_000_000
    max_label_delay_ns: int = 250_000_000
    seed: int = 42
    alphas: tuple[float, ...] = (0.1, 1.0, 10.0)
    min_samples_per_session: int = 64
    max_abs_prediction_bps: float = 1000.0

    def validate(self):
        if any(type(value) is not int or not 1_000_000 <= value <= 60_000_000_000
               for value in (self.horizon_ns, self.max_label_delay_ns)):
            raise ValueError("Target horizon and allowed label delay must be 1 ms to 60 s")
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError("Seed must be an unsigned 32-bit integer")
        if type(self.min_samples_per_session) is not int or not 32 <= self.min_samples_per_session <= 1_000_000:
            raise ValueError("Training needs at least 32 labeled samples in every session")
        if not isinstance(self.alphas, tuple) or not 1 <= len(self.alphas) <= 12 or any(type(a) not in (float, int) or not math.isfinite(a) or not 1e-6 <= a <= 1e6 for a in self.alphas):
            raise ValueError("Provide a bounded, positive, predefined Ridge alpha grid")
        if len(set(self.alphas)) != len(self.alphas):
            raise ValueError("Duplicate hyperparameters are not allowed")
        if type(self.max_abs_prediction_bps) not in (float, int) or not math.isfinite(self.max_abs_prediction_bps) or not 0 < self.max_abs_prediction_bps <= 10000:
            raise ValueError("Invalid forecast bound")
        return self


def _arrays(samples):
    import numpy as np
    return np.asarray([s.features for s in samples], dtype=np.float64), np.asarray([s.target_bps for s in samples], dtype=np.float64)


def _fit(samples, alpha, seed):
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    x, y = _arrays(samples)
    scaler = StandardScaler().fit(x)
    estimator = Ridge(alpha=alpha, solver="svd", random_state=seed).fit(scaler.transform(x), y)
    return scaler, estimator


def _metric(y, prediction):
    import numpy as np
    residual = prediction - y
    active = y != 0
    corr = None
    if len(y) > 1 and np.std(y) > 1e-12 and np.std(prediction) > 1e-12:
        corr = float(np.corrcoef(y, prediction)[0, 1])
    return {"samples": len(y), "mse_bps2": float(np.mean(residual ** 2)),
            "mae_bps": float(np.mean(np.abs(residual))), "rmse_bps": float(np.sqrt(np.mean(residual ** 2))),
            "correlation": corr,
            "direction_accuracy": float(np.mean(np.sign(prediction[active]) == np.sign(y[active]))) if np.any(active) else None,
            "nonzero_target_samples": int(np.sum(active))}


def _evaluate(samples, scaler, estimator):
    import numpy as np
    x, y = _arrays(samples)
    return {"ridge": _metric(y, estimator.predict(scaler.transform(x))),
            "zero": _metric(y, np.zeros_like(y)), "microprice": _metric(y, x[:, 2])}


def _purge(training, following, embargo_ns):
    boundary = min(sample.absolute_timestamp_ns for sample in following)
    retained = [sample for sample in training if sample.absolute_label_end_ns + embargo_ns < boundary]
    return retained, len(training) - len(retained)


def _flatten(sessions):
    return [sample for _, samples in sessions for sample in samples]


def _split_summary(sessions, samples):
    result = []
    for capture, _ in sessions:
        items = [sample for sample in samples if sample.capture_id == capture.capture_id]
        if not items:
            raise ValueError("Purge removed a complete session; acquire more independent data")
        result.append({"capture_id": capture.capture_id, "session_date": capture.session_date,
                       "first_timestamp_ns": items[0].timestamp_ns, "last_timestamp_ns": items[-1].timestamp_ns,
                       "first_label_end_ns": items[0].label_end_ns, "last_label_end_ns": items[-1].label_end_ns,
                       "samples": len(items)})
    return {"sessions": result, "samples": len(samples)}


def _reference(samples, scaler):
    import numpy as np
    x, _ = _arrays(samples)
    result = {}
    for index, name in enumerate(FEATURE_NAMES):
        edges = np.unique(np.quantile(x[:, index], np.linspace(0, 1, 11))).tolist()
        if len(edges) == 1:
            epsilon = max(abs(edges[0]) * 1e-9, 1e-9)
            edges = [edges[0] - epsilon, edges[0] + epsilon]
        counts, _ = np.histogram(x[:, index], bins=edges)
        result[name] = {"edges": edges, "counts": counts.tolist(), "mean": float(scaler.mean_[index]),
                        "scale": float(scaler.scale_[index])}
    return {"fit_scope": "training_only", "samples": len(samples), "features": result}


def _claim_holdout(dataset, test_captures, experiment_sha256):
    """Persist test consumption beside the original dataset, not an output run.

    This protects the supplied workflow, not hostile filesystem administrators.
    A changed experiment must acquire a later, untouched test interval.
    """
    ledger_path = dataset.path.parent / ".ml_holdout_registry.json"
    lock_path = dataset.path.parent / ".ml_holdout_registry.lock"
    if ledger_path.is_symlink() or lock_path.is_symlink():
        raise ValueError("Holdout registry must not use symbolic links")
    keys = sorted({f"sha256:{capture.itch_sha256}" for capture in test_captures} |
                  {f"session:{capture.symbol}:{capture.session_date}" for capture in test_captures})
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = {"schema_version": 1, "entries": []}
        if ledger_path.exists():
            if not ledger_path.is_file() or ledger_path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Invalid holdout registry")
            ledger = json.loads(ledger_path.read_text())
            if not isinstance(ledger, dict) or ledger.get("schema_version") != 1 or not isinstance(ledger.get("entries"), list):
                raise ValueError("Malformed holdout registry")
        for entry in ledger["entries"]:
            if set(keys).intersection(entry["test_keys"]):
                if keys == entry["test_keys"] and experiment_sha256 == entry["experiment_sha256"]:
                    return {"locked": True, "reuse_exact_reproduction": True, "experiment_sha256": experiment_sha256,
                            "test_source_sha256": [capture.itch_sha256 for capture in test_captures]}
                raise ValueError("Locked holdout already consumed by another experiment; provide later untouched sessions")
        entry = {"test_keys": keys, "experiment_sha256": experiment_sha256,
                 "dataset_sha256": dataset.dataset_sha256, "reserved_at_ns": time.time_ns()}
        ledger["entries"].append(entry)
        temporary = ledger_path.with_suffix(".json.tmp")
        if temporary.exists():
            raise ValueError("Interrupted holdout transaction requires operator inspection")
        write_json(temporary, ledger)
        os.replace(temporary, ledger_path)
        dirfd = os.open(ledger_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return {"locked": True, "reuse_exact_reproduction": False, "experiment_sha256": experiment_sha256,
                "test_source_sha256": [capture.itch_sha256 for capture in test_captures]}


def train_model(manifest_path, output_dir, config=None, feature_config=None, exporter=None):
    """Train, choose on validation, consume the locked holdout once, save evidence."""
    import numpy as np
    import sklearn
    config, feature_config = (config or TrainingConfig()).validate(), (feature_config or FeatureConfig()).validate()
    output = Path(output_dir)
    if output.exists():
        raise ValueError("Training output must be a new directory; artifacts are immutable")
    dataset = load_manifest(manifest_path)
    if len(dataset.captures) < 10:
        raise ValueError("At least ten independent declared sessions are required (minimum 6/2/2 split)")
    output.mkdir(parents=True, mode=0o700)
    started_ns = time.time_ns()
    log = output / "train.log"

    def record(event, **fields):
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp_ns": time.time_ns(), "event": event, **fields}, allow_nan=False) + "\n")

    try:
        np.random.seed(config.seed)
        lineage = verify_market_exports(dataset, exporter)
        record("dataset_verified", dataset_sha256=dataset.dataset_sha256, kind=dataset.provenance["kind"], sessions=len(dataset.captures))
        sessions = build_labeled_sessions(dataset, feature_config, config.horizon_ns, config.max_label_delay_ns)
        if any(len(samples) < config.min_samples_per_session for _, samples in sessions):
            raise ValueError("A session has too few valid causal labeled observations after warmup and gap filtering")
        train_end, val_end = int(len(sessions) * 0.6), int(len(sessions) * 0.8)
        train_sessions, validation_sessions, test_sessions = sessions[:train_end], sessions[train_end:val_end], sessions[val_end:]
        train, validation, test = map(_flatten, (train_sessions, validation_sessions, test_sessions))
        embargo_ns = config.horizon_ns + config.max_label_delay_ns
        train, train_removed = _purge(train, validation, embargo_ns)
        validation, val_removed = _purge(validation, test, embargo_ns)
        if not train or not validation or not test:
            raise ValueError("Insufficient observations after purge and embargo")
        split = {name: _split_summary(group, samples) for name, group, samples in
                 (("train", train_sessions, train), ("validation", validation_sessions, validation), ("test", test_sessions, test))}
        record("chronological_split", samples={name: value["samples"] for name, value in split.items()}, purge_removed=train_removed + val_removed)
        walk_forward, models, validation_candidates = [], {}, {}
        fold_boundaries = list(range(max(3, len(train_sessions) // 2), len(train_sessions)))[-3:]
        for boundary in fold_boundaries:
            fit_samples = _flatten(train_sessions[:boundary])
            future_samples = _flatten(train_sessions[boundary:boundary + 1])
            fit_samples, removed = _purge(fit_samples, future_samples, embargo_ns)
            fold = {"train_capture_ids": [c.capture_id for c, _ in train_sessions[:boundary]],
                    "validation_capture_ids": [train_sessions[boundary][0].capture_id], "purged_samples": removed,
                    "train_last_label_ns": fit_samples[-1].absolute_label_end_ns,
                    "validation_first_ns": future_samples[0].absolute_timestamp_ns, "alpha_metrics": {}}
            for alpha in config.alphas:
                scaler, estimator = _fit(fit_samples, alpha, config.seed)
                fold["alpha_metrics"][str(alpha)] = _evaluate(future_samples, scaler, estimator)["ridge"]
            walk_forward.append(fold)
        for alpha in config.alphas:
            scaler, estimator = _fit(train, alpha, config.seed)
            models[alpha] = (scaler, estimator)
            validation_candidates[str(alpha)] = _evaluate(validation, scaler, estimator)["ridge"]
        selected_alpha = min(config.alphas, key=lambda a: (
            validation_candidates[str(a)]["mse_bps2"],
            sum(fold["alpha_metrics"][str(a)]["mse_bps2"] for fold in walk_forward), a))
        scaler, estimator = models[selected_alpha]
        record("model_selected_on_validation", alpha=selected_alpha, validation_mse_bps2=validation_candidates[str(selected_alpha)]["mse_bps2"])
        configuration = {"training": asdict(config), "features": asdict(feature_config),
                         "selection": "validation_mse_then_training_walk_forward_mse", "split_fraction": [0.6, 0.2, 0.2]}
        reference = _reference(train, scaler)
        payload = {"schema_version": 1, "model_type": "ridge_mid_return_bps",
                   "features": {"schema_version": FEATURE_SCHEMA_VERSION, "names": list(FEATURE_NAMES), "config": asdict(feature_config)},
                   "preprocessing": {"mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist()},
                   "estimator": {"coefficients": estimator.coef_.tolist(), "intercept": float(estimator.intercept_), "alpha": selected_alpha},
                   "target": {"name": "future_mid_return_bps", "horizon_ns": config.horizon_ns,
                              "max_label_delay_ns": config.max_label_delay_ns, "max_abs_prediction_bps": config.max_abs_prediction_bps},
                   "provenance": {**dataset.provenance, "symbol": dataset.captures[0].symbol,
                                  "dataset_sha256": dataset.dataset_sha256},
                   "training": {"config": configuration, "seed": config.seed, "data_manifest_sha256": dataset.manifest_sha256, **lineage,
                                "dataset_sha256": dataset.dataset_sha256, "split": split, "feature_reference": reference,
                                "versions": {"python": platform.python_version(), "numpy": np.__version__, "sklearn": sklearn.__version__},
                                "features_fit_scope": "training_only", "target_units": "basis_points",
                                "market_time_assumption": "ordered_exchange_event_time; actual_receive_time_unavailable",
                                "selection_scope": "training_walk_forward_and_validation_only"}}
        # The experiment identity is sealed before any test predictions/metrics.
        experiment_sha256 = hashlib.sha256(canonical_bytes(payload)).hexdigest()
        holdout = _claim_holdout(dataset, [capture for capture, _ in test_sessions], experiment_sha256)
        record("holdout_reserved", **holdout)
        payload["model_version"] = artifact_version(payload)
        model = LinearModel(payload)
        metrics = {"train": _evaluate(train, scaler, estimator), "validation": _evaluate(validation, scaler, estimator),
                   "test": _evaluate(test, scaler, estimator)}
        # Validate the dependency-free deployed estimator against the training implementation.
        parity = max(abs(model.predict(sample.features) - float(estimator.predict(scaler.transform([sample.features]))[0])) for sample in test[:64])
        if parity > 1e-9:
            raise ValueError("Saved numerical model fails training/inference parity")
        model.save(output / "model.json")
        report = {"schema_version": 1, "model_version": model.model_version,
                  "model_sha256": sha256_file(output / "model.json"), "dataset_sha256": dataset.dataset_sha256,
                  "data_manifest_sha256": dataset.manifest_sha256, "provenance": dataset.provenance,
                  **lineage,
                  "trained_at_ns": started_ns, "completed_at_ns": time.time_ns(),
                  "split": split, "splits": {name: [s["capture_id"] for s in value["sessions"]] for name, value in split.items()},
                  "chronological_split": True, "purged_horizon": True,
                  "purge": {"horizon_ns": config.horizon_ns, "max_label_delay_ns": config.max_label_delay_ns,
                            "embargo_ns": embargo_ns, "removed_samples": train_removed + val_removed,
                            "label_cross_session_allowed": False, "label_cross_reset_allowed": False},
                  "walk_forward": walk_forward, "validation_candidates": validation_candidates,
                  "selected_alpha": selected_alpha, "metrics": metrics, "holdout": holdout,
                  "drift_reference": reference, "inference_parity_max_abs_bps": parity,
                  "eligible_for_paper": False, "approval_state": "unapproved_research_candidate",
                  "limitations": ["Regression forecasts are not calibrated probabilities or profitability evidence.",
                                  "Historical exchange timestamps do not measure source-to-receiver latency.",
                                  "Cost-aware queue/latency simulation is a separate required evaluation.",
                                  "Session dates are operator declarations; file names do not establish market provenance.",
                                  "Synthetic data, if used, validates software mechanics only."]}
        write_json(output / "report.json", report)
        write_json(output / "config.json", configuration)
        write_json(output / "data-manifest.json", dataset.raw)
        record("training_complete", model_version=model.model_version, inference_parity_max_abs_bps=parity,
               test_mse_bps2=metrics["test"]["ridge"]["mse_bps2"], eligible_for_paper=False)
        return report
    except Exception as exc:
        record("training_failed", error_type=type(exc).__name__, message=str(exc))
        raise
