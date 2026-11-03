"""Temporary contract fixtures only; no real-market or broker validation claims."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ml.data import load_manifest
from ml.features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FeatureConfig
from ml.model import LinearModel, artifact_version
from ml.valuation import aggregate_sessions, evaluation_contract, research_source_fingerprints
from ml.registry import DEFAULT_GATES, Registry, RegistryError, digest, load_approved_model, read_json, read_status

POLICY = {"signal_ttl_ns": 250_000_000, "min_expected_edge_bps": 1.0, "fee_per_share_units": 35,
          "min_fee_per_order_units": 10000, "slippage_buffer_units": 20, "max_target_position": 10, "allow_short": False}


def write(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    path.chmod(0o600)
    return path


def candidate(directory, *, kind="synthetic", start=date(2026, 1, 1), registry=None):
    """Build a coherent, small numerical artifact, with TEST-ONLY evidence."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    registry = registry or Registry(directory / "store")
    admission = registry.install_policy({"schema_version": 1, "policy": POLICY, "gates": dict(DEFAULT_GATES)})
    captures = []
    for index in range(10):
        day = (start + timedelta(days=index)).isoformat()
        source, market = directory / f"{day}.itch", directory / f"{day}.csv"
        # A registry test does not invoke the exporter; bytes are explicit fixtures.
        source.write_bytes(f"unit-contract-itch-{day}".encode())
        market.write_bytes(f"unit-contract-market-{day}".encode())
        captures.append({"capture_id": day, "session_date": day, "symbol": "TEST", "stock_locate": 1,
                         "itch_path": source.name, "itch_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                         "market_csv_path": market.name, "market_csv_sha256": hashlib.sha256(market.read_bytes()).hexdigest()})
    data_path = write(directory / "data.json", {"schema_version": 1,
                      "provenance": {"kind": kind, "source": "UNIT TEST CONTRACT ONLY", "authorized_use_attested": kind == "licensed_market"},
                      "captures": captures})
    dataset = load_manifest(data_path)
    split = {}
    for name, rows in (("train", captures[:6]), ("validation", captures[6:8]), ("test", captures[8:])):
        sessions = [{"capture_id": x["capture_id"], "session_date": x["session_date"], "samples": 100,
                     "first_timestamp_ns": 34_200_000_000_000, "last_timestamp_ns": 34_201_000_000_000,
                     "first_label_end_ns": 34_200_500_000_000, "last_label_end_ns": 34_201_500_000_000} for x in rows]
        split[name] = {"sessions": sessions, "samples": len(rows) * 100}
    reference = {"fit_scope": "training_only", "samples": 600, "features": {
        name: {"edges": [-2.0, 0.0, 2.0], "counts": [300, 300], "mean": 0.0, "scale": 1.0} for name in FEATURE_NAMES}}
    payload = {"schema_version": 1, "model_type": "ridge_mid_return_bps",
               "features": {"schema_version": FEATURE_SCHEMA_VERSION, "names": list(FEATURE_NAMES), "config": asdict(FeatureConfig())},
               "preprocessing": {"mean": [0.0] * len(FEATURE_NAMES), "scale": [1.0] * len(FEATURE_NAMES)},
               "estimator": {"coefficients": [0.01] * len(FEATURE_NAMES), "intercept": 0.0, "alpha": 1.0},
               "target": {"name": "future_mid_return_bps", "horizon_ns": 500_000_000,
                          "max_label_delay_ns": 250_000_000, "max_abs_prediction_bps": 100.0},
               "provenance": dataset.provenance,
               "training": {"data_manifest_sha256": dataset.manifest_sha256, "dataset_sha256": dataset.dataset_sha256,
                            "split": split, "feature_reference": reference, "market_exports_verified": True,
                            "exporter_sha256": "e" * 64, "seed": 42,
                            "config": {"training": {"alphas": [0.1, 1.0, 10.0]}}}}
    payload["model_version"] = artifact_version(payload)
    model_path = directory / "model.json"
    LinearModel(payload).save(model_path)
    version = registry.register(model_path, admission_sha256=admission, data_manifest=data_path)
    metrics = {}
    for region in split:
        metrics[region] = {}
        for name, rmse in (("ridge", 0.5), ("zero", 1.0), ("microprice", 0.8)):
            metrics[region][name] = {"rmse_bps": rmse, "mse_bps2": rmse**2, "mae_bps": rmse,
                                    "correlation": 0.5, "direction_accuracy": 0.75, "samples": split[region]["samples"]}
    trained_at = time.time_ns()
    training = {"schema_version": 1, "model_version": version, "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
                "data_manifest_sha256": dataset.manifest_sha256, "dataset_sha256": dataset.dataset_sha256,
                "provenance": dataset.provenance, "market_exports_verified": True, "exporter_sha256": "e" * 64,
                "trained_at_ns": trained_at, "completed_at_ns": trained_at, "split": split,
                "chronological_split": True, "purged_horizon": True, "metrics": metrics, "selected_alpha": 1.0,
                "purge": {"horizon_ns": 500_000_000, "max_label_delay_ns": 250_000_000,
                          "embargo_ns": 750_000_000, "label_cross_session_allowed": False, "label_cross_reset_allowed": False},
                "holdout": {"locked": True, "reuse_exact_reproduction": False,
                            "experiment_sha256": digest({k: v for k, v in payload.items() if k != "model_version"}),
                            "test_source_sha256": [x["itch_sha256"] for x in captures[8:]]}}
    input_hashes = {x["capture_id"]: x["itch_sha256"] for x in captures[8:]}
    plan = {**evaluation_contract(), "split": "test", "model_sha256": training["model_sha256"],
            "policy_sha256": digest(POLICY), "admission_sha256": admission, "engine_sha256": "e" * 64,
            "dataset_sha256": dataset.dataset_sha256, "maximum_order_shares": 1,
            "daily_loss_limit_cents": 10000, "new_order_rate": 5, "latency_ns": 1_000_000,
            "cancel_latency_ns": 1_000_000, "python_source_sha256": research_source_fingerprints()}
    def scenario(multiplier=1, latency=1):
        rows = []
        for capture in captures[8:]:
            # These coherent accounting figures exercise registry contracts only.
            # Actual training/FIFO integration is tested separately with C++.
            fees = 100000 * multiplier
            realized = 105000 if multiplier == 1 else 195000
            net = realized - fees
            config = {"fee_per_share_units": 35 * multiplier, "min_fee_per_order_units": 10000 * multiplier,
                      "slippage_buffer_units": 20 * multiplier, "rebate_per_share_units": 0,
                      "participation_gate_required": True, "long_only": True, "max_signal_age_ns": 250_000_000,
                      "trade_start_ns": 34_200_000_000_000, "trade_end_ns": 34_201_500_000_001,
                      "max_order_size": 1, "orders_per_second": 5, "daily_loss_limit_units": 1_000_000,
                      "daily_drawdown_limit_units": 1_000_000, "hard_inventory_limit": 10,
                      "soft_inventory_limit": 5, "latency_ns": 1_000_000 * latency,
                      "quote_lifetime_ns": 1_000_000_000, "tick_size": 100, "min_net_edge_units": 0,
                      "cancel_latency_ns": 1_000_000 * latency}
            ledger = {"orders_submitted": 10, "fill_count": 10, "filled_quantity": 10, "realized_pnl_units": realized,
                      "unrealized_pnl_units": 0, "fees_units": fees, "rebates_units": 0,
                      "cash_before_fees_units": realized, "net_pnl_units": net,
                      "drawdown_units": 10000 * multiplier, "inventory": 0, "open_orders_at_end": 0,
                      "pnl_mark_valid": True, "killed": False, "config": config,
                      "signal_gate": {"signals_applied": 1000},
                      "execution_metrics": {"buy_quantity": 5, "sell_quantity": 5,
                          "buy_notional_units": 5_000_000, "sell_notional_units": 5_000_000 + realized,
                          "turnover_units": 10_000_000 + realized, "closed_inventory_episodes": 5,
                          "holding_total_ns": 500_000_000, "peak_abs_inventory_shares": 1,
                          "inventory_share_seconds": 0.5},
                      "valuation": {"schema_version": 1, "basis": "fresh_bbo", "quote_mark_valid": True,
                          "net_pnl_valid": True, "unrealized_pnl_units": 0, "net_pnl_units": net,
                          "drawdown_complete": True, "max_drawdown_units": 10000 * multiplier}}
            engine = {"book_valid": True, "messages": 2000, "summary": {"mode": "simulation", "orders_sent": 0,
                      "fills_simulated": 10, "simulation": ledger}}
            rows.append({"capture_id": capture["capture_id"], "session_date": capture["session_date"],
                         "events": 1000, "start_ns": config["trade_start_ns"], "end_ns": config["trade_end_ns"],
                         "simulation": ledger, "engine_report": engine, "engine_report_sha256": digest(engine)})
        return rows, aggregate_sessions(rows)
    rows, metrics = scenario()
    stress_rows, stress_metrics = scenario(2)
    latency_rows, latency_metrics = scenario(latency=5)
    wait_rows, _ = scenario()
    for row in wait_rows:
        ledger = row["simulation"]
        for name in ("orders_submitted", "fill_count", "filled_quantity", "realized_pnl_units", "fees_units",
                     "cash_before_fees_units", "net_pnl_units", "drawdown_units"):
            ledger[name] = 0
        for name in ledger["execution_metrics"]:
            ledger["execution_metrics"][name] = 0
        ledger["valuation"]["net_pnl_units"] = 0
        ledger["valuation"]["max_drawdown_units"] = 0
        row["engine_report"]["summary"]["fills_simulated"] = 0
        row["engine_report_sha256"] = digest(row["engine_report"])
    wait_metrics = aggregate_sessions(wait_rows)
    simulation = {"schema_version": 2, "report_type": "simulation_validation", "model_version": version,
                  "model_sha256": training["model_sha256"], "policy_sha256": digest(POLICY),
                  "admission_sha256": admission, "evaluation_plan": plan, "evaluation_plan_sha256": digest(plan),
                  "python_source_sha256": plan["python_source_sha256"],
                  "input_sha256": digest(input_hashes), "input_hashes": input_hashes, "capture_ids": list(input_hashes),
                  "engine_sha256": "e" * 64, "provenance_kind": kind, "chronological_holdout": True,
                  "execution_authorized": False,
                  "time_boundaries": {"training_end_date": captures[7]["session_date"],
                                      "simulation_start_date": captures[8]["session_date"], "simulation_end_date": captures[9]["session_date"]},
                  "metrics": metrics, "simulation_summary": metrics, "by_session": rows,
                  "baselines": {"wait": wait_metrics, "cost_aware_two_sided": metrics},
                  "baseline_by_session": {"wait": wait_rows, "cost_aware_two_sided": rows},
                  "cost_stress": [{"multiplier": 2, "fee_per_share_units": 70,
                     "min_fee_per_order_units": 20000, "slippage_buffer_units": 40,
                     "metrics": stress_metrics, "by_session": stress_rows}],
                  "latency_stress": {"multiplier": 5, "metrics": latency_metrics, "by_session": latency_rows}}
    return {"registry": registry, "version": version, "model": model_path, "data": data_path,
            "training": training, "simulation": simulation}


def approval(item, path, *, action="approve_paper", from_version=None):
    registry, version = item["registry"], item["version"]
    manifest = registry._manifest(version)
    training, _ = registry._event(version, "evaluated")
    simulation, _ = registry._event(version, "simulation_validated")
    value = {"schema_version": 1, "action": action, "approved": True, "version": version,
             "operator_id": "unit-test-operator", "issued_at_ns": time.time_ns(), "data_authorization_confirmed": True,
             "model_sha256": manifest["model_sha256"], "policy_sha256": manifest["policy_sha256"],
             "gates_sha256": manifest["gates_sha256"], "data_manifest_sha256": manifest["data_manifest_sha256"],
             "validation_report_sha256": training["report_sha256"], "simulation_report_sha256": simulation["report_sha256"]}
    if from_version:
        value["from_version"] = from_version
    return write(path, value)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def valid_simulation(self, *, kind="synthetic"):
        item = candidate(self.root / "data", kind=kind)
        item["registry"].evaluate(item["version"], item["training"])
        item["registry"].validate_simulation(item["version"], item["simulation"])
        return item

    def test_candidate_cannot_load_or_activate(self):
        item = candidate(self.root / "data")
        with self.assertRaises(RegistryError):
            load_approved_model(item["registry"].root)
        with self.assertRaises(RegistryError):
            item["registry"].activate(item["version"], mode="simulation")
        with self.assertRaisesRegex(RegistryError, "Synthetic"):
            item["registry"].activate(item["version"], mode="paper")
        self.assertEqual(read_status(item["registry"].root)["models"][0]["stage"], "candidate")

    def test_synthetic_only_activates_simulation(self):
        item = self.valid_simulation()
        registry, version = item["registry"], item["version"]
        registry.activate(version, mode="simulation")
        self.assertEqual(load_approved_model(registry.root, "simulation").version, version)
        operator = approval(item, self.root / "approval.json")
        with self.assertRaisesRegex(RegistryError, "Synthetic"):
            registry.approve_paper(version, operator, data_manifest=item["data"])
        with self.assertRaises(RegistryError):
            load_approved_model(registry.root, "live")

    def test_paper_requires_explicit_approval_and_separate_activation(self):
        item = self.valid_simulation(kind="licensed_market")
        registry, version = item["registry"], item["version"]
        with self.assertRaises(RegistryError):
            registry.activate(version, mode="paper")
        operator = approval(item, self.root / "approval.json")
        registry.approve_paper(version, operator, data_manifest=item["data"])
        self.assertFalse((registry.root / "active-paper.json").exists())
        registry.activate(version, mode="paper", operator_file=operator)
        loaded = load_approved_model(registry.root, "paper")
        self.assertEqual(loaded.sha256, item["training"]["model_sha256"])
        self.assertEqual(loaded.manifest["policy"], POLICY)

    def test_paper_rejects_post_training_threshold_installation(self):
        item = candidate(self.root / "data", kind="licensed_market")
        manifest = item["registry"]._manifest(item["version"])
        item["training"]["trained_at_ns"] = manifest["policy_registered_at_ns"] - 1000
        item["registry"].evaluate(item["version"], item["training"])
        item["registry"].validate_simulation(item["version"], item["simulation"])
        operator = approval(item, self.root / "approval.json")
        with self.assertRaisesRegex(RegistryError, "frozen before"):
            item["registry"].approve_paper(item["version"], operator, data_manifest=item["data"])

    def test_approval_requires_boolean_and_private_permissions(self):
        item = self.valid_simulation(kind="licensed_market")
        operator = approval(item, self.root / "approval.json")
        value = json.loads(operator.read_text())
        value["approved"] = 1
        write(operator, value)
        with self.assertRaisesRegex(RegistryError, "boolean"):
            item["registry"].approve_paper(item["version"], operator, data_manifest=item["data"])
        value["approved"] = True
        write(operator, value)
        operator.chmod(0o644)
        with self.assertRaisesRegex(RegistryError, "private"):
            item["registry"].approve_paper(item["version"], operator, data_manifest=item["data"])

    def test_failed_simulation_is_visible_but_cannot_promote(self):
        item = candidate(self.root / "data")
        registry, version = item["registry"], item["version"]
        registry.evaluate(version, item["training"])
        item["simulation"]["metrics"].update(fills=0, net_pnl_units=None, max_drawdown_units=None, pnl_mark_valid=False)
        with self.assertRaises(RegistryError):
            registry.validate_simulation(version, item["simulation"])
        status = read_status(registry.root)
        self.assertTrue(status["available"])
        self.assertEqual(status["models"][0]["stage"], "evaluated")
        self.assertFalse(status["models"][0]["simulation_passed"])
        self.assertIsNone(status["models"][0]["simulation_summary"]["net_pnl_units"])

    def test_cost_stress_cannot_lie_about_multiplier(self):
        item = candidate(self.root / "data")
        item["registry"].evaluate(item["version"], item["training"])
        item["simulation"]["cost_stress"][0]["fee_per_share_units"] = 35
        with self.assertRaisesRegex(RegistryError, "actual configured costs"):
            item["registry"].validate_simulation(item["version"], item["simulation"])

    def test_schema_one_evidence_is_display_only_and_cannot_promote(self):
        item = candidate(self.root / "data")
        registry, version = item["registry"], item["version"]
        registry.evaluate(version, item["training"])
        item["simulation"]["schema_version"] = 1
        with self.assertRaises(RegistryError) as failure:
            registry.validate_simulation(version, item["simulation"])
        self.assertEqual(failure.exception.failure_codes, ("LEGACY_REPORT_REQUIRES_REVALIDATION",))
        status = read_status(registry.root)
        self.assertTrue(status["available"])
        self.assertFalse(status["models"][0]["simulation_passed"])
        self.assertEqual(status["models"][0]["simulation_failure_codes"], ["LEGACY_REPORT_REQUIRES_REVALIDATION"])

    def test_historical_schema_one_activation_cannot_bypass_new_load_validation(self):
        item = self.valid_simulation()
        registry, version = item["registry"], item["version"]
        registry.activate(version, mode="simulation")
        # Construct a coherent historical event, not a corrupt hash. This models
        # an old store already activated before the stricter release was installed.
        legacy = json.loads(json.dumps(item["simulation"]))
        legacy["schema_version"] = 1
        identity = digest(legacy)
        write(registry.root / "evidence" / f"{identity}.json", legacy)
        path = registry.root / "events" / version / "simulation_validated.json"
        event = json.loads(path.read_text())
        event["report_sha256"] = identity
        path.chmod(0o600)
        write(path, event)
        with self.assertRaises(RegistryError) as failure:
            load_approved_model(registry.root, "simulation")
        self.assertEqual(failure.exception.failure_codes, ("LEGACY_REPORT_REQUIRES_REVALIDATION",))

    def test_previously_approved_model_requires_current_research_source_on_load(self):
        item = self.valid_simulation()
        registry = item["registry"]
        registry.activate(item["version"], mode="simulation")
        changed = {**research_source_fingerprints(), "valuation.py": "0" * 64}
        with patch("ml.registry.research_source_fingerprints", return_value=changed):
            with self.assertRaisesRegex(RegistryError, "source fingerprints"):
                load_approved_model(registry.root, "simulation")

    def test_profitable_summary_cannot_override_the_engine_session_ledgers(self):
        item = candidate(self.root / "data")
        item["registry"].evaluate(item["version"], item["training"])
        item["simulation"]["metrics"]["net_pnl_units"] += 1_000_000
        with self.assertRaises(RegistryError) as failure:
            item["registry"].validate_simulation(item["version"], item["simulation"])
        self.assertEqual(failure.exception.failure_codes, ("AGGREGATE_MISMATCH",))

    def test_selected_subset_cannot_replace_complete_locked_test(self):
        item = candidate(self.root / "data")
        item["registry"].evaluate(item["version"], item["training"])
        report = item["simulation"]
        report["capture_ids"] = report["capture_ids"][:1]
        report["input_hashes"] = {key: report["input_hashes"][key] for key in report["capture_ids"]}
        report["input_sha256"] = digest(report["input_hashes"])
        with self.assertRaises(RegistryError) as failure:
            item["registry"].validate_simulation(item["version"], report)
        self.assertEqual(failure.exception.failure_codes, ("HOLDOUT_MISMATCH",))

    def test_cost_stress_must_cover_all_sessions_even_with_matching_subset_aggregate(self):
        item = candidate(self.root / "data")
        item["registry"].evaluate(item["version"], item["training"])
        stress = item["simulation"]["cost_stress"][0]
        stress["by_session"] = stress["by_session"][:1]
        stress["metrics"] = aggregate_sessions(stress["by_session"])
        with self.assertRaises(RegistryError) as failure:
            item["registry"].validate_simulation(item["version"], item["simulation"])
        self.assertEqual(failure.exception.failure_codes, ("HOLDOUT_MISMATCH",))

    def test_raw_engine_hash_and_source_fingerprints_are_verified(self):
        for mutation in ("raw_hash", "research_source"):
            with self.subTest(mutation=mutation):
                item = candidate(self.root / mutation)
                item["registry"].evaluate(item["version"], item["training"])
                report = item["simulation"]
                if mutation == "raw_hash":
                    report["by_session"][0]["engine_report_sha256"] = "0" * 64
                else:
                    report["evaluation_plan"]["python_source_sha256"]["valuation.py"] = "0" * 64
                    report["evaluation_plan_sha256"] = digest(report["evaluation_plan"])
                with self.assertRaises(RegistryError):
                    item["registry"].validate_simulation(item["version"], report)

    def test_self_consistent_alternate_execution_assumptions_are_rejected(self):
        for name, value in (("latency_ns", 1), ("maximum_order_shares", 100), ("scenarios", ["model"])):
            with self.subTest(name=name):
                item = candidate(self.root / name)
                item["registry"].evaluate(item["version"], item["training"])
                report = item["simulation"]
                report["evaluation_plan"][name] = value
                report["evaluation_plan_sha256"] = digest(report["evaluation_plan"])
                with self.assertRaisesRegex(RegistryError, "fixed research contract"):
                    item["registry"].validate_simulation(item["version"], report)

    def test_latency_scenario_cannot_omit_stress_or_hide_risk_failure(self):
        for mutation in ("no_stress", "killed", "drawdown"):
            with self.subTest(mutation=mutation):
                item = candidate(self.root / mutation)
                item["registry"].evaluate(item["version"], item["training"])
                stress = item["simulation"]["latency_stress"]
                if mutation == "no_stress":
                    stress["multiplier"] = 1
                else:
                    for row in stress["by_session"]:
                        if mutation == "killed":
                            row["simulation"]["killed"] = True
                        else:
                            row["simulation"]["valuation"]["max_drawdown_units"] = 1_000_001
                        row["engine_report_sha256"] = digest(row["engine_report"])
                    stress["metrics"] = aggregate_sessions(stress["by_session"])
                with self.assertRaises(RegistryError) as failure:
                    item["registry"].validate_simulation(item["version"], item["simulation"])
                expected = {"no_stress": "INVALID_EVIDENCE", "killed": "RISK_KILLED", "drawdown": "DRAWDOWN_LIMIT"}
                self.assertIn(expected[mutation], failure.exception.failure_codes)

    def test_wait_baseline_cannot_relabel_a_profitable_strategy(self):
        item = candidate(self.root / "data")
        item["registry"].evaluate(item["version"], item["training"])
        report = item["simulation"]
        report["baselines"]["wait"] = report["metrics"]
        report["baseline_by_session"]["wait"] = report["by_session"]
        with self.assertRaisesRegex(RegistryError, "WAIT baseline"):
            item["registry"].validate_simulation(item["version"], report)

    def test_simulation_cannot_claim_external_execution_authority(self):
        item = candidate(self.root / "data")
        item["registry"].evaluate(item["version"], item["training"])
        item["simulation"]["execution_authorized"] = True
        with self.assertRaises(RegistryError):
            item["registry"].validate_simulation(item["version"], item["simulation"])

    def test_holdout_cannot_be_reused_for_a_changed_estimator(self):
        item = candidate(self.root / "data")
        registry = item["registry"]
        registry.evaluate(item["version"], item["training"])
        payload = LinearModel.load(item["model"]).to_dict()
        payload["estimator"]["intercept"] = 0.01
        payload["model_version"] = artifact_version(payload)
        changed = self.root / "changed.json"
        LinearModel(payload).save(changed)
        version = registry.register(changed, admission_sha256=registry._manifest(item["version"])["admission_sha256"], data_manifest=item["data"])
        report = json.loads(json.dumps(item["training"]))
        report.update(model_version=version, model_sha256=hashlib.sha256(changed.read_bytes()).hexdigest())
        report["holdout"]["experiment_sha256"] = digest({k: v for k, v in payload.items() if k != "model_version"})
        with self.assertRaisesRegex(RegistryError, "different experiment"):
            registry.evaluate(version, report)

    def test_open_exposure_blocks_promotion(self):
        item = candidate(self.root / "data")
        item["registry"].evaluate(item["version"], item["training"])
        item["simulation"]["metrics"]["open_exposure"] = True
        with self.assertRaisesRegex(RegistryError, "aggregate"):
            item["registry"].validate_simulation(item["version"], item["simulation"])

    def test_unverified_export_lineage_is_rejected(self):
        item = candidate(self.root / "data")
        item["training"]["market_exports_verified"] = False
        with self.assertRaisesRegex(RegistryError, "export lineage"):
            item["registry"].evaluate(item["version"], item["training"])

    def test_registration_never_overwrites_different_policy_binding(self):
        item = candidate(self.root / "data")
        changed = {"schema_version": 1, "policy": {**POLICY, "max_target_position": 11}, "gates": dict(DEFAULT_GATES)}
        identity = item["registry"].install_policy(changed)
        with self.assertRaisesRegex(RegistryError, "different bindings"):
            item["registry"].register(item["model"], admission_sha256=identity, data_manifest=item["data"])

    def test_model_tamper_invalidates_load_and_status(self):
        item = self.valid_simulation()
        registry, version = item["registry"], item["version"]
        registry.activate(version)
        path = registry._version(version) / "model.json"
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(RegistryError, "hash mismatch"):
            load_approved_model(registry.root, "simulation")
        self.assertEqual(read_status(registry.root)["state"], "invalid")

    def test_dangling_pointer_fifo_and_world_writable_root_rejected(self):
        item = candidate(self.root / "data")
        registry = item["registry"]
        pointer = registry.root / "active-paper.json"
        pointer.symlink_to(self.root / "missing")
        self.assertEqual(read_status(registry.root)["state"], "invalid")
        pointer.unlink()
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(RegistryError):
            read_json(fifo)
        registry.root.chmod(0o777)
        self.assertEqual(read_status(registry.root)["state"], "invalid")
        registry.root.chmod(0o700)

    def test_json_duplicate_nan_and_traversal_rejected(self):
        for name, raw in (("duplicate", '{"a":1,"a":2}'), ("nan", '{"a":NaN}')):
            path = self.root / name
            path.write_text(raw)
            with self.assertRaises(RegistryError):
                read_json(path)
        item = candidate(self.root / "data")
        with self.assertRaises(RegistryError):
            item["registry"]._manifest("../../etc/passwd")

    def test_controlled_rollback_preserves_both_versions(self):
        first = self.valid_simulation()
        registry = first["registry"]
        registry.activate(first["version"])
        second = candidate(self.root / "new", start=date(2026, 2, 1), registry=registry)
        registry.evaluate(second["version"], second["training"])
        registry.validate_simulation(second["version"], second["simulation"])
        registry.activate(second["version"])
        command = approval(first, self.root / "rollback.json", action="rollback", from_version=second["version"])
        pointer = registry.rollback("simulation", first["version"], command)
        self.assertEqual(pointer["previous_version"], second["version"])
        self.assertTrue(registry._version(first["version"]).is_dir())
        self.assertTrue(registry._version(second["version"]).is_dir())


if __name__ == "__main__":
    unittest.main()
