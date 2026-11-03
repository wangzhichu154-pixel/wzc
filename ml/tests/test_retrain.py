"""Drift math and a real scheduler -> training subprocess -> candidate lifecycle."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ml.data import load_manifest
from ml.features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FeatureConfig
from ml.fixtures import generate_dataset
from ml.model import LinearModel, write_json
from ml.prepare import prepare
from ml.registry import DEFAULT_GATES, Registry, RegistryError, _write, read_bytes, read_json
from ml.retrain import RetrainConfig, detect_drift, migrate_attempts, tick
from ml.reservations import LEDGER_PATH, read_ledger, reserve
from ml.runtime import PolicyConfig
from ml.training import TrainingConfig, train_model

ROOT = Path(__file__).resolve().parents[2]
EXPORTER = ROOT / "build" / "trading-ml-replay"


@unittest.skipUnless(EXPORTER.is_file(), "Build the actual C++ exporter before retraining tests")
class RetrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        generate_dataset(cls.root / "base-data", sessions=10, steps=128, seed=17)
        cls.base_manifest = prepare(cls.root / "base-data" / "source-manifest.json", EXPORTER)
        cls.training_config = TrainingConfig(horizon_ns=300_000_000, max_label_delay_ns=50_000_000,
                                             seed=123, alphas=(2.0, 7.0), max_abs_prediction_bps=99.0)
        cls.feature_config = replace(FeatureConfig(), window_events=16, max_gap_ns=1_500_000_000,
                                     regular_open_ns=FeatureConfig().regular_open_ns + 1_000_000,
                                     regular_close_ns=FeatureConfig().regular_close_ns - 60_000_000_000)
        train_model(cls.base_manifest, cls.root / "base-training", cls.training_config, cls.feature_config)
        cls.model_path = cls.root / "base-training" / "model.json"
        cls.model = LinearModel.load(cls.model_path)
        for name, seed, start in (("new-data", 29, "2026-07-01"), ("old-date-data", 31, "2026-06-01")):
            generate_dataset(cls.root / name, sessions=10, steps=128, seed=seed, start_date=start)
            prepare(cls.root / name / "source-manifest.json", EXPORTER)
        cls.new_manifest = cls.root / "new-data" / "manifest.json"

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.directory = self.root / self._testMethodName
        self.directory.mkdir()
        self.registry = Registry(self.directory / "registry")
        self.admission = self.registry.install_policy({"schema_version": 1,
            "policy": PolicyConfig().policy_document(), "gates": DEFAULT_GATES})
        self.version = self.registry.register(self.model_path, admission_sha256=self.admission, data_manifest=self.base_manifest)
        self.registered_ns = self.registry._manifest(self.version)["registered_at_ns"]
        self.due_ns = self.registered_ns + 2_000_000_000
        self.config = RetrainConfig(interval_seconds=1)

    def _tick(self, **changes):
        arguments = {"base_version": self.version, "data_manifest": self.new_manifest,
                     "config": self.config, "allow_synthetic": True, "now_ns": self.due_ns}
        arguments.update(changes)
        return tick(self.registry.root, **arguments)

    def _window(self, samples, observed_at_ns=None):
        destination = self.directory / "window.json"
        write_json(destination, {"schema_version": 1, "model_version": self.version,
                   "feature_schema_version": FEATURE_SCHEMA_VERSION, "feature_names": list(FEATURE_NAMES),
                   "observed_at_ns": observed_at_ns if observed_at_ns is not None else self.registered_ns,
                   "samples": samples})
        return destination

    def test_insufficient_window_has_no_drift_conclusion_or_training(self):
        drift = detect_drift(self.model, [list(self.model.mean)], self.config)
        self.assertFalse(drift["evaluated"])
        self.assertIsNone(drift["drift_detected"])
        window = self._window([list(self.model.mean)])
        result = self._tick(feature_window=window, now_ns=self.registered_ns, execute=True)
        self.assertEqual(result["state"], "insufficient_data")
        self.assertFalse((self.registry.root / "retrain" / "attempts.json").exists())

    def test_standardized_mean_or_out_of_support_psi_triggers_drift(self):
        samples = [list(self.model.mean) for _ in range(200)]
        for row in samples:
            row[0] += 3 * self.model.scale[0]
        drift = detect_drift(self.model, samples, self.config)
        self.assertTrue(drift["evaluated"])
        self.assertTrue(drift["drift_detected"])
        self.assertGreaterEqual(drift["features"][FEATURE_NAMES[0]]["standardized_mean_shift"], 2)
        # Symmetric observations can preserve the mean but still leave support.
        samples = [list(self.model.mean) for _ in range(200)]
        for index, row in enumerate(samples):
            row[0] += (1 if index % 2 else -1) * self.model.scale[0] * 100
        drift = detect_drift(self.model, samples, self.config)
        self.assertAlmostEqual(drift["features"][FEATURE_NAMES[0]]["standardized_mean_shift"], 0, places=9)
        self.assertGreater(drift["features"][FEATURE_NAMES[0]]["psi"], self.config.psi_threshold)
        window = self._window(samples)
        result = self._tick(feature_window=window, now_ns=self.registered_ns)
        self.assertEqual(result["state"], "requested")
        request = read_json(self.registry.root / "retrain" / "requests" / f"{result['request_id']}.json")
        self.assertEqual(request["reason"], "FEATURE_DRIFT")
        self.assertFalse(request["automatic_promotion"])

    def test_periodic_dry_run_is_idempotent_and_does_not_consume_attempt(self):
        first, second = self._tick(), self._tick()
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["state"], "requested")
        self.assertEqual(len(list((self.registry.root / "retrain" / "requests").glob("*.json"))), 1)
        self.assertFalse((self.registry.root / "retrain" / "attempts.json").exists())
        self.assertFalse((self.registry.root / "active-paper.json").exists())
        self.assertFalse((self.registry.root / LEDGER_PATH).exists())

    def test_pipeline_reservation_blocks_periodic_training_before_subprocess(self):
        with self.registry._lock():
            reserve(self.registry, load_manifest(self.new_manifest), owner="pipeline", request_sha256="a" * 64)
        with patch("ml.retrain.subprocess.run", side_effect=AssertionError("Cross-entrypoint holdout reuse must be blocked")):
            result = self._tick(execute=True, exporter=EXPORTER)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "DATA_ALREADY_ATTEMPTED")
        self.assertFalse((self.registry.root / "retrain/attempts.json").exists())
        self.assertEqual(len(read_ledger(self.registry)["requests"]), 1)

    def test_missing_unauthorized_same_or_old_test_data_are_blocked(self):
        self.assertEqual(self._tick(data_manifest=None)["reason"], "NO_NEW_AUTHORIZED_DATA")
        self.assertEqual(self._tick(allow_synthetic=False)["state"], "blocked")
        self.assertEqual(self._tick(data_manifest=self.base_manifest)["reason"], "DATA_ALREADY_ATTEMPTED")
        old = self._tick(data_manifest=self.root / "old-date-data" / "manifest.json")
        self.assertEqual(old["state"], "blocked")
        self.assertEqual(old["reason"], "DATA_ALREADY_ATTEMPTED")

    def test_symbol_change_is_rejected_before_training(self):
        raw = json.loads(self.new_manifest.read_text())
        for item in raw["captures"]:
            item["symbol"] = "OTHER"
        destination = self.new_manifest.parent / "wrong-symbol.json"
        write_json(destination, raw)
        result = self._tick(data_manifest=destination, execute=True)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], "SYMBOL_MISMATCH")
        self.assertFalse((self.registry.root / "retrain" / "attempts.json").exists())

    def test_stale_or_bad_window_is_rejected(self):
        window = self._window([list(self.model.mean)] * 200, observed_at_ns=self.registered_ns - 7200_000_000_000)
        with self.assertRaises(RegistryError):
            self._tick(feature_window=window)
        with self.assertRaises(RegistryError):
            detect_drift(self.model, [[float("nan")] * 6] * 200)

    def test_dangling_scheduler_control_file_is_not_treated_as_absent(self):
        (self.registry.root / "retrain" / "schedule.json").symlink_to(self.directory / "missing-schedule")
        with self.assertRaises(RegistryError):
            self._tick(execute=True, exporter=EXPORTER)
        self.assertFalse((self.registry.root / "retrain" / "attempts.json").exists())

    def test_actual_retraining_preserves_all_frozen_config_and_only_registers_candidate(self):
        result = self._tick(execute=True, exporter=EXPORTER)
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["reason"], "CREATED_CANDIDATE")
        self.assertFalse(result["automatic_promotion"])
        new_model = self.registry._check_model(result["model_version"])
        self.assertEqual(new_model.training_metadata["config"], self.model.training_metadata["config"])
        self.assertEqual(new_model.feature_config, self.feature_config)
        self.assertEqual(new_model.training_metadata["config"]["training"]["alphas"], [2.0, 7.0])
        self.assertNotEqual(new_model.model_version, self.version)
        index = read_json(self.registry.root / "index.json")
        self.assertTrue(all(row["stage"] == "candidate" for row in index["models"]))
        self.assertFalse(list(self.registry.root.glob("active-*.json")))
        run = self.registry.root / "retrain" / "runs" / result["request_id"]
        self.assertTrue((run / "stdout.log").is_file())
        self.assertTrue((run / "stderr.log").is_file())
        self.assertIn('"success": true', (run / "stdout.log").read_text())
        # An already trained dataset is not retried after the next period.
        repeated = self._tick(execute=True, exporter=EXPORTER, now_ns=time.time_ns() + 2_000_000_000)
        self.assertEqual(repeated["reason"], "DATA_ALREADY_ATTEMPTED")

    def test_subprocess_failure_is_recorded_and_not_silently_retried(self):
        first = self._tick(execute=True, exporter=self.directory / "missing-exporter")
        self.assertEqual(first["state"], "failed")
        reservation = next(iter(read_ledger(self.registry)["requests"].values()))
        self.assertEqual(reservation["owner"], "retrain")
        self.assertEqual(reservation["dataset_sha256"], load_manifest(self.new_manifest).dataset_sha256)
        run = self.registry.root / "retrain" / "runs" / first["request_id"]
        failure = read_json(run / "failure.json")
        self.assertEqual(failure["stage"], "training")
        self.assertEqual(failure["returncode"], 2)
        self.assertIn("Build trading-ml-replay", (run / "stderr.log").read_text())
        second = self._tick(execute=True, exporter=EXPORTER)
        self.assertEqual(second["reason"], "DATA_ALREADY_ATTEMPTED")
        attempted = read_json(self.registry.root / "retrain" / "attempts.json")
        self.assertIn(load_manifest(self.new_manifest).dataset_sha256, attempted["datasets"])
        changed = json.loads(self.new_manifest.read_text())
        changed["provenance"]["source"] += "; changed description does not make the test fresh"
        changed_path = self.new_manifest.parent / "changed-description.json"
        write_json(changed_path, changed)
        third = self._tick(data_manifest=changed_path, execute=True, exporter=EXPORTER)
        self.assertEqual(third["reason"], "DATA_ALREADY_ATTEMPTED")

    def test_successful_legacy_ledger_migration_verifies_candidates_and_preserves_backup(self):
        result = self._tick(execute=True, exporter=EXPORTER)
        self.assertEqual(result["state"], "completed")
        path = self.registry.root / "retrain" / "attempts.json"
        current = read_json(path)
        _write(path, {"schema_version": 1, "datasets": current["datasets"]}, replace=True)
        original = read_bytes(path)
        upgraded = migrate_attempts(self.registry.root)
        self.assertTrue(upgraded["migrated"])
        self.assertEqual(upgraded["schema_version"], 2)
        self.assertEqual(read_bytes(path.parent / upgraded["backup"]), original)
        migrated = read_json(path)
        self.assertEqual(migrated["datasets"], current["datasets"])
        self.assertEqual(migrated["holdout_keys"], current["holdout_keys"])
        self.assertFalse(migrate_attempts(self.registry.root)["migrated"])

    def test_failed_legacy_attempt_cannot_be_migrated_by_guessing_or_clearing_it(self):
        result = self._tick(execute=True, exporter=self.directory / "missing-exporter")
        self.assertEqual(result["state"], "failed")
        path = self.registry.root / "retrain" / "attempts.json"
        current = read_json(path)
        _write(path, {"schema_version": 1, "datasets": current["datasets"]}, replace=True)
        original = read_bytes(path)
        with self.assertRaises((RegistryError, OSError)):
            migrate_attempts(self.registry.root)
        self.assertEqual(read_bytes(path), original)
        self.assertFalse(list(path.parent.glob("attempts.v1.*.json")))


if __name__ == "__main__":
    unittest.main()
