"""Numerical JSON model artifacts; prediction never loads executable pickle."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from .features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FeatureConfig


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def artifact_version(payload):
    return "ridge-" + hashlib.sha256(canonical_bytes({key: value for key, value in payload.items() if key != "model_version"})).hexdigest()[:24]


def write_json(path, value):
    path = Path(path)
    data = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False).encode() + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON model field")
        result[key] = value
    return result


def _finite(value, *, positive=False, magnitude=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > magnitude or (positive and value <= 0):
        raise ValueError("Invalid numerical model parameter")
    return float(value)


class LinearModel:
    """One immutable coefficient/scaler snapshot; no runtime ML dependency."""

    def __init__(self, payload):
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1 or payload.get("model_type") != "ridge_mid_return_bps":
            raise ValueError("Unsupported model schema or estimator")
        required = {"schema_version", "model_type", "model_version", "features", "preprocessing", "estimator", "target", "provenance", "training"}
        if set(payload) != required:
            raise ValueError("Model artifact fields differ from the supported schema")
        if payload["model_version"] != artifact_version(payload):
            raise ValueError("Model version digest does not match artifact contents")
        features = payload["features"]
        if not isinstance(features, dict) or set(features) != {"schema_version", "names", "config"}:
            raise ValueError("Invalid feature schema")
        if features["schema_version"] != FEATURE_SCHEMA_VERSION or features["names"] != list(FEATURE_NAMES):
            raise ValueError("Unknown or reordered model features")
        self.feature_config = FeatureConfig.from_dict(features["config"])
        self.feature_names = FEATURE_NAMES
        pre, est, target = payload["preprocessing"], payload["estimator"], payload["target"]
        if not isinstance(pre, dict) or set(pre) != {"mean", "scale"} or not isinstance(est, dict) or set(est) != {"coefficients", "intercept", "alpha"}:
            raise ValueError("Invalid linear estimator parameters")
        if not all(isinstance(x, list) and len(x) == len(FEATURE_NAMES) for x in (pre["mean"], pre["scale"], est["coefficients"])):
            raise ValueError("Model dimension mismatch")
        self.mean = tuple(_finite(x) for x in pre["mean"])
        self.scale = tuple(_finite(x, positive=True) for x in pre["scale"])
        self.coefficients = tuple(_finite(x) for x in est["coefficients"])
        self.intercept = _finite(est["intercept"])
        self.alpha = _finite(est["alpha"], positive=True)
        if not isinstance(target, dict) or set(target) != {"name", "horizon_ns", "max_label_delay_ns", "max_abs_prediction_bps"} or target["name"] != "future_mid_return_bps":
            raise ValueError("Invalid prediction target")
        for key in ("horizon_ns", "max_label_delay_ns"):
            if type(target[key]) is not int or not 1_000_000 <= target[key] <= 60_000_000_000:
                raise ValueError("Unsupported target horizon")
        self.horizon_ns = target["horizon_ns"]
        self.max_label_delay_ns = target["max_label_delay_ns"]
        self.max_abs_prediction_bps = _finite(target["max_abs_prediction_bps"], positive=True, magnitude=10000)
        provenance, training = payload["provenance"], payload["training"]
        if not isinstance(provenance, dict) or provenance.get("kind") not in ("synthetic", "licensed_market") or not isinstance(training, dict):
            raise ValueError("Model data provenance is required")
        self._payload = json.loads(canonical_bytes(payload))
        self.model_version = payload["model_version"]
        self.artifact_file_sha256 = None
        self.provenance = self._payload["provenance"]
        self.training_metadata = self._payload["training"]

    @classmethod
    def load(cls, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Model must be a bounded regular JSON file")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            return cls.from_bytes(stream.read(2 * 1024 * 1024 + 1))

    @classmethod
    def from_bytes(cls, raw):
        if type(raw) is not bytes or not 0 < len(raw) <= 2 * 1024 * 1024:
            raise ValueError("Model must be bounded JSON bytes")
        try:
            payload = json.loads(raw, object_pairs_hook=_unique_pairs,
                                 parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON constant")))
            model = cls(payload)
            model.artifact_file_sha256 = hashlib.sha256(raw).hexdigest()
            return model
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("Malformed model artifact") from exc

    def predict(self, features):
        if not isinstance(features, (tuple, list)) or len(features) != len(FEATURE_NAMES):
            raise ValueError("Prediction requires the exact ordered feature schema")
        values = [_finite(value) for value in features]
        answer = self.intercept + math.fsum(w * ((x - m) / s) for x, m, s, w in zip(values, self.mean, self.scale, self.coefficients))
        if not math.isfinite(answer) or abs(answer) > self.max_abs_prediction_bps:
            raise ValueError("Prediction outside the configured supported range")
        return answer

    def to_dict(self):
        return json.loads(canonical_bytes(self._payload))

    def save(self, path):
        write_json(path, self._payload)
        self.artifact_file_sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
