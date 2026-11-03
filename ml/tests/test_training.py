import csv
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ml.data import (CSV_FIELDS, Capture, build_labeled_sessions, label_capture, load_manifest,
                     sha256_file, verify_market_exports)
from ml.features import FeatureConfig, REGULAR_OPEN_NS
from ml.fixtures import generate_dataset
from ml.model import LinearModel, canonical_bytes, write_json
from ml.training import TrainingConfig, train_model

ROOT = Path(__file__).resolve().parents[2]
EXPORTER = ROOT / "build" / "trading-ml-replay"


def prepare_fixture(directory, sessions=10, steps=128):
    manifest = generate_dataset(directory, sessions=sessions, steps=steps)
    for index, capture in enumerate(manifest["captures"]):
        relative = f"captures/market-{index}.csv"
        completed = subprocess.run([str(EXPORTER), "--input", str(directory / capture["itch_path"]),
                                    "--symbol-locate", "1", "--export-market", str(directory / relative)],
                                   capture_output=True, text=True, timeout=20)
        if completed.returncode:
            raise AssertionError(completed.stderr)
        capture["market_csv_path"] = relative
        capture["market_csv_sha256"] = sha256_file(directory / relative)
    write_json(directory / "manifest.json", manifest)
    return directory / "manifest.json"


class LabelIsolationTests(unittest.TestCase):
    def test_labels_never_cross_halt_or_use_unbounded_future(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "market.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for index in range(1, 80):
                    now = REGULAR_OPEN_NS + index * 100_000_000
                    writer.writerow({"ordinal": index, "timestamp_ns": now, "logical_timestamp_ns": now,
                                     "stock_locate": 1, "bid": 1_000_000 + index * 100,
                                     "ask": 1_000_400 + index * 100, "bid_qty": 100 + index,
                                     "ask_qty": 100, "valid": 1, "tradable": int(index != 40)})
            capture = Capture("test", "2026-06-01", "TEST", 1, path, "a" * 64, path, "b" * 64)
            samples = label_capture(capture, FeatureConfig(window_events=4), 500_000_000, 100_000_000)
            boundary = REGULAR_OPEN_NS + 4_000_000_000
            self.assertTrue(samples)
            self.assertTrue(all(not (sample.timestamp_ns < boundary < sample.label_end_ns) for sample in samples))
            self.assertTrue(all(500_000_000 <= sample.label_end_ns - sample.timestamp_ns <= 600_000_000 for sample in samples))
            self.assertLessEqual(samples[-1].timestamp_ns, REGULAR_OPEN_NS + 7_400_000_000)
            self.assertEqual({sample.segment for sample in samples}, {0, 1})


@unittest.skipUnless(EXPORTER.is_file(), "Build the real C++ trading-ml-replay exporter first")
class ActualTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.manifest = prepare_fixture(cls.root / "dataset")
        cls.report = train_model(cls.manifest, cls.root / "run")
        cls.model = LinearModel.load(cls.root / "run" / "model.json")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_training_chronology_baselines_and_train_only_scaler(self):
        import numpy as np
        report = self.report
        self.assertEqual([len(report["split"][name]["sessions"]) for name in ("train", "validation", "test")], [6, 2, 2])
        self.assertEqual(report["provenance"]["kind"], "synthetic")
        self.assertFalse(report["eligible_for_paper"])
        self.assertTrue(report["market_exports_verified"])
        self.assertLess(report["inference_parity_max_abs_bps"], 1e-9)
        self.assertEqual(set(report["metrics"]["test"]), {"ridge", "zero", "microprice"})
        self.assertEqual(len(report["walk_forward"]), 3)
        dataset = load_manifest(self.manifest)
        labeled = build_labeled_sessions(dataset, self.model.feature_config, self.model.horizon_ns, self.model.max_label_delay_ns)
        train_x = np.asarray([sample.features for _, samples in labeled[:6] for sample in samples])
        np.testing.assert_allclose(self.model.mean, np.mean(train_x, axis=0), rtol=1e-12)
        self.assertEqual(report["model_sha256"], sha256_file(self.root / "run" / "model.json"))
        for fold in report["walk_forward"]:
            self.assertLess(fold["train_last_label_ns"], fold["validation_first_ns"])

    def test_exact_reproduction_and_changed_experiment_holdout_refusal(self):
        duplicate = train_model(self.manifest, self.root / "reproduction")
        self.assertTrue(duplicate["holdout"]["reuse_exact_reproduction"])
        self.assertEqual(sha256_file(self.root / "run" / "model.json"), sha256_file(self.root / "reproduction" / "model.json"))
        with self.assertRaisesRegex(ValueError, "Locked holdout"):
            train_model(self.manifest, self.root / "forbidden-retune", replace(TrainingConfig(), alphas=(100.0,)))
        self.assertFalse((self.root / "forbidden-retune" / "model.json").exists())

    def test_verified_manifest_rejects_hash_duplicates_and_duplicate_sessions(self):
        raw = json.loads(self.manifest.read_text())
        for name, mutate in (("duplicate-hash", lambda v: v["captures"][1].update(itch_sha256=v["captures"][0]["itch_sha256"])),
                             ("duplicate-session", lambda v: v["captures"][1].update(session_date=v["captures"][0]["session_date"]))):
            clone = json.loads(canonical_bytes(raw))
            mutate(clone)
            destination = self.manifest.parent / (name + ".json")
            write_json(destination, clone)
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                load_manifest(destination)
        clone = json.loads(canonical_bytes(raw))
        clone["captures"][0]["market_csv_sha256"] = "0" * 64
        destination = self.manifest.parent / "wrong-hash.json"
        write_json(destination, clone)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            load_manifest(destination)

    def test_csv_with_new_hash_but_wrong_source_lineage_is_refused(self):
        dataset = load_manifest(self.manifest)
        capture = dataset.captures[0]
        destination = self.manifest.parent / "fabricated.csv"
        content = capture.market_csv_path.read_text().replace("1000000", "1000100")
        if content == capture.market_csv_path.read_text():
            content += "\n"
        destination.write_text(content)
        modified = replace(capture, market_csv_path=destination, market_csv_sha256=sha256_file(destination))
        with self.assertRaisesRegex(ValueError, "does not match the original ITCH"):
            verify_market_exports(replace(dataset, captures=(modified,)))

    def test_safe_json_artifact_and_strict_numeric_predictions(self):
        raw = (self.root / "run" / "model.json").read_bytes()
        self.assertEqual(LinearModel.from_bytes(raw).model_version, self.model.model_version)
        self.assertEqual(self.model.artifact_file_sha256, sha256_file(self.root / "run" / "model.json"))
        for invalid in (b'{"schema_version":1,"schema_version":1}', b"NaN", b"cos\nsystem\n(S'echo bad'\ntR."):
            with self.assertRaises(ValueError):
                LinearModel.from_bytes(invalid)
        with self.assertRaises(ValueError):
            self.model.predict([float("nan")] * 6)
        with self.assertRaises(ValueError):
            self.model.predict([0.0] * 5)
        tampered = self.model.to_dict()
        tampered["estimator"]["intercept"] += 1
        with self.assertRaisesRegex(ValueError, "digest"):
            LinearModel(tampered)

    def test_fixture_is_reproducible_and_distinct_per_session(self):
        generated = generate_dataset(self.root / "same-fixture", sessions=10, steps=128)
        original = load_manifest(self.manifest)
        self.assertEqual([capture.itch_sha256 for capture in original.captures], [entry["itch_sha256"] for entry in generated["captures"]])
        self.assertEqual(len({entry["itch_sha256"] for entry in generated["captures"]}), 10)
        self.assertFalse(generated["provenance"]["authorized_use_attested"])

    def test_new_synthetic_start_date_and_seed_create_distinct_future_holdout(self):
        generated = generate_dataset(self.root / "future-fixture", sessions=10, steps=128, seed=43, start_date="2026-07-01")
        self.assertEqual(generated["captures"][0]["session_date"], "2026-07-01")
        original = load_manifest(self.manifest)
        self.assertGreater(min(entry["session_date"] for entry in generated["captures"]), max(c.session_date for c in original.captures))
        self.assertFalse(set(entry["itch_sha256"] for entry in generated["captures"]) & set(c.itch_sha256 for c in original.captures))


if __name__ == "__main__":
    unittest.main()
