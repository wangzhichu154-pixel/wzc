"""Actual C++/Python training-to-simulation integration; no broker or fake fills."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ml.backtest import backtest
from ml.data import load_manifest, sha256_file
from ml.fixtures import generate_dataset
from ml.model import write_json
from ml.prepare import prepare
from ml.registry import DEFAULT_GATES, Registry, RegistryError, read_status
from ml.runtime import PolicyConfig
from ml.training import train_model

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "build" / "trading-ml-replay"


@unittest.skipUnless(EXPORTER.is_file(), "Build trading-ml-replay before integration tests")
class TrainingToSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        generate_dataset(cls.root / "dataset", sessions=10, steps=128, seed=17)
        cls.manifest = prepare(cls.root / "dataset" / "source-manifest.json", EXPORTER)
        cls.training = cls.root / "training"
        cls.training_report = train_model(cls.manifest, cls.training)
        cls.policy = PolicyConfig()
        cls.admission = cls.root / "admission.json"
        write_json(cls.admission, {"schema_version": 1, "policy": cls.policy.policy_document(), "gates": DEFAULT_GATES})
        cls.simulation = cls.root / "simulation"
        cls.report = backtest(cls.manifest, cls.training, cls.simulation, EXPORTER, cls.admission)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_model_waits_when_expected_returns_do_not_cover_predeclared_costs(self):
        report = self.report
        self.assertGreater(report["signal_reason_counts"]["EXPECTED_EDGE_BELOW_COST"], 0)
        self.assertEqual(report["metrics"]["fills"], 0)
        self.assertEqual(report["baselines"]["wait"]["fill_count"], 0)
        self.assertEqual(report["metrics"]["fees_units"], 0)
        self.assertEqual(report["metrics"]["realized_pnl_units"], 0)
        self.assertFalse(report["metrics"]["pnl_mark_valid"])
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["metrics"]["net_pnl_units"], 0)
        self.assertEqual(report["metrics"]["unrealized_pnl_units"], 0)
        self.assertEqual(report["metrics"]["max_drawdown_units"], 0)
        self.assertTrue(report["metrics"]["valuation"]["net_pnl_valid"])
        self.assertTrue(report["metrics"]["valuation"]["drawdown_complete"])
        self.assertEqual(report["metrics"]["valuation"]["basis"], "reconciled_flat_ledger")
        self.assertIsNone(report["metrics"]["return_on_capital"])
        self.assertEqual(report["broker_latency"], {"status": "unmeasured", "orders_sent": 0})
        self.assertFalse(report["execution_authorized"])
        self.assertEqual(report["provenance_kind"], "synthetic")
        self.assertGreater(report["measured_internal_python_feature_and_policy"]["samples"], 0)
        self.assertIn("excludes NIC, broker, ACK", report["measured_internal_python_feature_and_policy"]["scope"])
        for capture_id in report["capture_ids"]:
            lines = (self.simulation / f"{capture_id}-signals.jsonl").read_text().splitlines()
            signals = [json.loads(line) for line in lines]
            self.assertTrue(all(signal["action"] == "WAIT" for signal in signals))
            self.assertTrue(all(not signal["allow_bid"] and not signal["allow_ask"] for signal in signals))

    def test_baseline_is_separate_and_independent_long_only_is_enforced(self):
        for capture_id in self.report["capture_ids"]:
            model_csv = (self.simulation / f"{capture_id}-model.csv").read_text()
            baseline_csv = (self.simulation / f"{capture_id}-baseline.csv").read_text()
            self.assertNotEqual(model_csv, baseline_csv)
            self.assertIn(",1,1,", baseline_csv)
            for scenario in ("model", "cost_aware_two_sided", "wait", "double_cost", "fivefold_latency"):
                engine = json.loads((self.simulation / f"{capture_id}-{scenario}-engine.json").read_text())
                simulation = engine.get("simulation", engine.get("summary", {}).get("simulation"))
                self.assertTrue(simulation["config"]["participation_gate_required"])
                self.assertTrue(simulation["config"]["long_only"])
                self.assertGreaterEqual(simulation["inventory"], 0)
        self.assertEqual(self.report["cost_stress"][0]["min_fee_per_order_units"], self.policy.min_fee_per_order_units * 2)
        self.assertEqual(self.report["latency_stress"]["multiplier"], 5)

    def test_report_binds_actual_model_inputs_signals_and_untouched_split(self):
        dataset = load_manifest(self.manifest)
        captures = {capture.capture_id: capture for capture in dataset.captures}
        self.assertEqual(self.report["model_sha256"], sha256_file(self.training / "model.json"))
        self.assertEqual(self.report["capture_ids"], self.training_report["splits"]["test"])
        self.assertTrue(self.report["chronological_holdout"])
        self.assertEqual(self.report["engine_sha256"], sha256_file(EXPORTER))
        self.assertEqual(self.report["policy_sha256"], self.policy.sha256)
        for row in self.report["inputs"]:
            capture = captures[row["capture_id"]]
            self.assertEqual(row["itch_sha256"], sha256_file(capture.itch_path))
            self.assertEqual(row["market_csv_sha256"], sha256_file(capture.market_csv_path))
            binding = json.loads((self.simulation / f"{capture.capture_id}-binding.json").read_text())
            for name, identity in binding["signals"].items():
                self.assertEqual(identity, sha256_file(self.simulation / f"{capture.capture_id}-{name}.csv"))

    def test_same_plan_reproduction_preserves_economic_results(self):
        repeated = backtest(self.manifest, self.training, self.root / "repeated-simulation", EXPORTER, self.admission)
        self.assertEqual(repeated["metrics"], self.report["metrics"])
        self.assertEqual(repeated["baselines"], self.report["baselines"])
        # Raw engine reports also preserve measured wall-clock performance, which
        # must not be mistaken for deterministic strategy/accounting output.
        self.assertEqual(repeated["cost_stress"][0]["metrics"], self.report["cost_stress"][0]["metrics"])
        self.assertEqual(repeated["latency_stress"]["metrics"], self.report["latency_stress"]["metrics"])
        for scenario in ("by_session",):
            self.assertEqual([row["simulation"] for row in repeated[scenario]],
                             [row["simulation"] for row in self.report[scenario]])
        self.assertEqual(repeated["signal_reason_counts"], self.report["signal_reason_counts"])

    def test_actual_engine_ledgers_reach_registry_and_zero_fills_block_promotion(self):
        registry = Registry(self.root / "actual-engine-registry")
        identity = registry.install_policy(json.loads(self.admission.read_text()))
        version = registry.register(self.training / "model.json", admission_sha256=identity, data_manifest=self.manifest)
        registry.evaluate(version, self.training_report)
        with self.assertRaises(RegistryError) as failure:
            registry.validate_simulation(version, self.report)
        self.assertIn("NO_FILLS", failure.exception.failure_codes)
        self.assertNotIn("AGGREGATE_MISMATCH", failure.exception.failure_codes)
        self.assertNotIn("INCOMPLETE_VALUATION", failure.exception.failure_codes)
        status = read_status(registry.root)
        model = status["models"][0]
        self.assertFalse(model["simulation_passed"])
        self.assertIn("NO_FILLS", model["simulation_failure_codes"])
        self.assertEqual(model["simulation_summary"]["net_pnl_units"], 0)
        self.assertFalse((registry.root / "active-simulation.json").exists())

    def test_policy_change_cannot_reuse_consumed_test(self):
        path = self.root / "retuned-admission.json"
        changed = replace(self.policy, min_expected_edge_bps=2.0)
        write_json(path, {"schema_version": 1, "policy": changed.policy_document(), "gates": DEFAULT_GATES})
        with self.assertRaisesRegex(ValueError, "already evaluated"):
            backtest(self.manifest, self.training, self.root / "forbidden-retune", EXPORTER, path)
        self.assertFalse(any((self.root / "forbidden-retune").glob("*-engine.json")))

    def _tampered_training(self, name, update):
        directory = self.root / name
        directory.mkdir()
        (directory / "model.json").write_bytes((self.training / "model.json").read_bytes())
        report = json.loads((self.training / "report.json").read_text())
        update(report)
        write_json(directory / "report.json", report)
        return directory

    def test_training_report_model_sha_tampering_is_rejected(self):
        tampered = self._tampered_training("tampered-hash", lambda report: report.update(model_sha256="0" * 64))
        with self.assertRaises(ValueError):
            backtest(self.manifest, tampered, self.root / "forbidden-hash", EXPORTER, self.admission)

    def test_training_sessions_cannot_be_relabelled_as_unseen_test(self):
        def alter(report):
            report["splits"]["test"] = report["splits"]["train"][:2]
        tampered = self._tampered_training("tampered-split", alter)
        with self.assertRaises(ValueError):
            backtest(self.manifest, tampered, self.root / "forbidden-split", EXPORTER, self.admission)


if __name__ == "__main__":
    unittest.main()
