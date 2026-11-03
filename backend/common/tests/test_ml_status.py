"""Read-only model monitor tests; fixture metrics are synthetic test values."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from common.ml_status import get_ml_status, ml_display_context


VERSION = "ridge-" + "a" * 24


def status_fixture():
    metric = {"mse_bps2": 1.0, "mae_bps": 0.5, "rmse_bps": 1.0,
              "correlation": None, "direction_accuracy": None, "samples": 20}
    training = {"split": {split: {"sessions": [{"capture_id": "/private/capture.itch", "samples": 20}], "samples": 20}
                            for split in ("train", "validation", "test")},
                "metrics": {split: {estimator: dict(metric) for estimator in ("ridge", "zero", "microprice")}
                            for split in ("train", "validation", "test")}, "selected_alpha": 1.0,
                "capture_path": "/private/training-secret.itch", "token": "do-not-expose"}
    return {"schema_version": 1, "available": True, "state": "ready", "live_enabled": False,
            "active": {"simulation": VERSION, "paper": None},
            "models": [{"version": VERSION, "stage": "trained", "provenance_kind": "synthetic",
                        "registered_at_ns": 1780000000000000000, "model_sha256": "b" * 64, "policy_sha256": "c" * 64,
                        "training_summary": training, "simulation_summary": None,
                        "metadata": {"account_id": "PRIVATE-ACCOUNT", "secret": "PRIVATE-TOKEN"}}],
            "retrain": {"state": "requested", "reason": "/private/secret-and-account", "updated_at_ns": 1780000000000000001}}


def account_summary(*, net=0, realized=0, fees=0, fills=0, complete=True):
    """Explicit synthetic ledger for projection validation; never trading data."""
    return {"realized_pnl_units": realized, "unrealized_pnl_units": 0, "fees_units": fees,
            "rebates_units": 0, "net_pnl_units": net, "max_drawdown_units": fees if complete else None,
            "cash_before_fees_units": realized, "inventory": 0, "open_orders_at_end": 0,
            "open_exposure": False, "pnl_mark_valid": False, "killed": False,
            "fill_count": fills, "filled_quantity": fills, "fills": fills,
            "residual_inventory_by_session": {"/private/synthetic-session": 0},
            "valuation": {"schema_version": 1, "basis": "reconciled_flat_ledger",
                          "quote_mark_valid": False, "net_pnl_valid": True, "drawdown_complete": complete,
                          "unrealized_pnl_units": 0, "net_pnl_units": net,
                          "max_drawdown_units": fees if complete else None}}


@override_settings(RATE_LIMIT_ENABLED=False, EMAIL_DELIVERY_EAGER=False)
class MLStatusTests(TestCase):
    def setUp(self):
        cache.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override = override_settings(ML_MODEL_STORE_DIR=self.temp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.operator = get_user_model().objects.create_user(email="ml-operator@example.test", password="Synthetic#Operator123",
                                                             is_staff=True, role="admin", email_verified=True)
        self.user = get_user_model().objects.create_user(email="ml-user@example.test", password="Synthetic#Ordinary123", email_verified=True)

    def projected(self, value=None):
        with patch("common.ml_status._read_registry", return_value=status_fixture() if value is None else value):
            return get_ml_status()

    @override_settings(ML_MODEL_STORE_DIR="")
    def test_unconfigured_never_claims_model_is_trained(self):
        result = get_ml_status()
        self.assertEqual(result["state"], "not_configured")
        self.assertFalse(result["available"])
        self.assertEqual(result["models"], [])
        self.assertFalse(result["execution_authorized"])

    def test_relative_traversal_or_missing_root_fails_closed(self):
        for root in ("../models", "models", self.temp.name + "/../models", self.temp.name + "/missing"):
            with self.subTest(root=root), override_settings(ML_MODEL_STORE_DIR=root), patch("common.ml_status._read_registry") as reader:
                result = get_ml_status()
                self.assertFalse(result["available"])
                reader.assert_not_called()
                self.assertNotIn(self.temp.name, json.dumps(result))

    def test_symlink_root_and_symlink_ancestor_rejected(self):
        root = Path(self.temp.name)
        actual = root / "actual"
        actual.mkdir()
        (actual / "child").mkdir()
        link = root / "linked"
        link.symlink_to(actual, target_is_directory=True)
        for value in (str(link), str(link / "child")):
            with self.subTest(root=value), override_settings(ML_MODEL_STORE_DIR=value), patch("common.ml_status._read_registry") as reader:
                self.assertFalse(get_ml_status()["available"])
                reader.assert_not_called()

    def test_regular_file_cannot_be_model_store_root(self):
        path = Path(self.temp.name) / "not-a-directory"
        path.write_text("{}")
        with override_settings(ML_MODEL_STORE_DIR=str(path)):
            self.assertEqual(get_ml_status()["state"], "invalid")

    def test_reader_failure_does_not_leak_diagnostics_or_paths(self):
        with patch("common.ml_status._read_registry", side_effect=OSError("private /capture/path and SECRET")):
            result = get_ml_status()
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_strict_allowlist_omits_paths_accounts_secrets_and_reason(self):
        result = self.projected()
        self.assertTrue(result["available"])
        text = json.dumps(result)
        for private in ("/private/", "PRIVATE-ACCOUNT", "PRIVATE-TOKEN", "do-not-expose", "capture_id", "reason"):
            self.assertNotIn(private, text)
        self.assertEqual(result["models"][0]["training_summary"]["split"]["train"]["sessions"], 1)
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["models"][0]["execution_authorized"])
        self.assertEqual(result["models"][0]["provenance_label"], "合成测试数据")

    def test_licensed_provenance_is_distinct_from_synthetic(self):
        value = status_fixture()
        value["models"][0]["provenance_kind"] = "licensed_market"
        result = self.projected(value)
        self.assertEqual(result["models"][0]["provenance_kind"], "licensed_market")
        self.assertNotEqual(result["models"][0]["provenance_label"], "合成测试数据")

    def test_missing_reports_remain_unrecorded(self):
        value = status_fixture()
        value["models"][0].pop("training_summary")
        result = self.projected(value)
        self.assertTrue(result["available"])
        self.assertIsNone(result["models"][0]["training_summary"])
        self.assertIsNone(result["models"][0]["simulation_summary"])

    def test_malformed_schema_capability_and_models_fail_closed(self):
        changes = [lambda v: v.update(schema_version=True), lambda v: v.update(live_enabled=True),
                   lambda v: v.update(available="true"), lambda v: v.update(models=[{}]), lambda v: v.update(active={}),
                   lambda v: v["models"][0].update(version="../../private.json"),
                   lambda v: v["models"][0].update(stage="unreviewed-secret"),
                   lambda v: v["models"][0].update(provenance_kind="unknown"),
                   lambda v: v["models"][0].update(model_sha256="../secret"),
                   lambda v: v["models"][0].update(registered_at_ns=True)]
        for index, change in enumerate(changes):
            value = status_fixture()
            change(value)
            with self.subTest(case=index):
                result = self.projected(value)
                self.assertFalse(result["available"])
                self.assertEqual(result["models"], [])

    def test_duplicate_versions_and_model_count_are_bounded(self):
        value = status_fixture()
        value["models"] *= 2
        self.assertFalse(self.projected(value)["available"])
        value["models"] *= 26
        self.assertFalse(self.projected(value)["available"])

    def test_invalid_metrics_and_missing_fields_do_not_become_zero(self):
        for metric in (float("nan"), float("inf"), True, -1):
            value = status_fixture()
            value["models"][0]["training_summary"]["metrics"]["test"]["ridge"]["mse_bps2"] = metric
            with self.subTest(metric=metric):
                self.assertFalse(self.projected(value)["available"])
        value = status_fixture()
        del value["models"][0]["training_summary"]["metrics"]["test"]["ridge"]["mse_bps2"]
        self.assertFalse(self.projected(value)["available"])

    def test_null_correlation_stays_null_not_fake_accuracy(self):
        result = self.projected()
        metrics = result["models"][0]["training_summary"]["metrics"]["test"]["ridge"]
        self.assertIsNone(metrics["correlation"])
        self.assertIsNone(metrics["direction_accuracy"])
        rows = ml_display_context(result)["models"][0]["metric_rows"]
        self.assertEqual(len(rows), 9)

    def test_anonymous_operator_and_ordinary_permissions(self):
        self.assertEqual(self.client.get("/api/v1/ai/status/").status_code, 401)
        self.assertEqual(self.client.get("/ai/").status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/v1/ai/status/").status_code, 403)
        self.assertEqual(self.client.get("/ai/").status_code, 403)
        self.client.force_login(self.operator)
        with patch("common.ml_status._read_registry", return_value=status_fixture()):
            response = self.client.get("/api/v1/ai/status/")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["data"]["available"])
            self.assertIn("no-store", response["Cache-Control"])

    def test_unverified_and_deleted_operators_are_denied(self):
        self.client.force_login(self.operator)
        get_user_model().objects.filter(pk=self.operator.pk).update(email_verified=False)
        self.assertEqual(self.client.get("/api/v1/ai/status/").status_code, 403)
        get_user_model().objects.filter(pk=self.operator.pk).update(email_verified=True, is_active=False)
        self.assertEqual(self.client.get("/api/v1/ai/status/").status_code, 401)

    def test_status_endpoint_rejects_mutation(self):
        self.client.force_login(self.operator)
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)("/api/v1/ai/status/", data="{}", content_type="application/json")
                self.assertEqual(response.status_code, 405)
                self.assertFalse(response.json()["data"]["execution_authorized"])

    def test_page_labels_synthetic_data_and_missing_simulation_evidence(self):
        self.client.force_login(self.operator)
        with patch("common.ml_status._read_registry", return_value=status_fixture()):
            response = self.client.get("/ai/")
        self.assertContains(response, "合成测试数据")
        self.assertContains(response, "不代表")
        self.assertContains(response, "未记录可展示的成本后模拟报告")
        self.assertContains(response, "不可用")
        self.assertNotContains(response, "PRIVATE-TOKEN")
        self.assertNotContains(response, "/private/")

    def test_invalid_registry_api_returns_unavailable_not_empty_success(self):
        self.client.force_login(self.operator)
        with patch("common.ml_status._read_registry", return_value={"bad": "secret"}):
            response = self.client.get("/api/v1/ai/status/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "ML_STATUS_UNAVAILABLE")
        self.assertFalse(response.json()["data"]["execution_authorized"])

    def test_query_string_cannot_select_registry_path(self):
        self.client.force_login(self.operator)
        with patch("common.ml_status._read_registry", return_value=status_fixture()) as reader:
            response = self.client.get("/api/v1/ai/status/?path=/etc/passwd&version=../../secret")
        self.assertEqual(response.status_code, 200)
        reader.assert_called_once_with(Path(self.temp.name))

    def test_no_model_versions_is_explicit_empty_state(self):
        value = status_fixture()
        value.update(models=[], active={"simulation": None, "paper": None}, retrain=None)
        result = self.projected(value)
        self.assertEqual(result["state"], "empty")
        self.assertEqual(result["models"], [])
        self.assertFalse(result["execution_authorized"])

    def test_partial_cost_summary_never_fills_missing_fees_with_zero(self):
        value = status_fixture()
        value["models"][0]["simulation_summary"] = {"net_pnl_units": 42, "max_drawdown_units": 11, "events": 100, "fills": 2, "pnl_mark_valid": True, "killed": False}
        result = self.projected(value)
        self.assertTrue(result["available"])
        simulation = result["models"][0]["simulation_summary"]
        self.assertFalse(simulation["ledger_complete"])
        self.assertNotIn("fees_units", simulation)
        self.assertIsNone(simulation["filled_quantity"])
        self.assertEqual(simulation["net_pnl_units"], 42)
        self.assertEqual(simulation["drawdown_scope"], "max_single_scenario")

    def test_invalid_mark_hides_all_estimated_net_and_drawdown_fields(self):
        value = status_fixture()
        value["models"][0]["simulation_summary"] = {"net_pnl_units": 4200, "max_drawdown_units": 900, "pnl_mark_valid": False, "killed": True}
        result = self.projected(value)
        simulation = result["models"][0]["simulation_summary"]
        for key in ("net_pnl_units", "drawdown_units", "max_drawdown_units", "unrealized_pnl_units"):
            self.assertIsNone(simulation[key])
        self.assertTrue(simulation["killed"])
        self.assertFalse(result["execution_authorized"])

    def test_complete_cost_ledger_must_reconcile(self):
        value = status_fixture()
        ledger = {"realized_pnl_units": 6000, "unrealized_pnl_units": 0, "fees_units": 1800, "rebates_units": 0, "net_pnl_units": 4200, "pnl_mark_valid": True, "killed": False, "filled_quantity": 60}
        value["models"][0]["simulation_summary"] = ledger
        result = self.projected(value)
        self.assertTrue(result["models"][0]["simulation_summary"]["ledger_complete"])
        ledger["net_pnl_units"] += 1
        self.assertFalse(self.projected(value)["available"])

    def test_negative_cost_or_nonfinite_summary_is_invalid(self):
        for value in ({"fees_units": -1}, {"net_pnl_units": float("inf")}, {"pnl_mark_valid": 1}, {"secret_only": "/private/path"}):
            state = status_fixture()
            state["models"][0]["simulation_summary"] = value
            with self.subTest(summary=value):
                self.assertFalse(self.projected(state)["available"])

    def test_actual_json_reader_rejects_duplicate_keys_nonfinite_and_invalid_json(self):
        from ml.registry import RegistryError, read_json
        path = Path(self.temp.name) / "report.json"
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"broken":', b'[]'):
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(RegistryError):
                read_json(path)

    def test_actual_json_reader_enforces_two_megabyte_bound(self):
        from ml.registry import RegistryError, read_json
        path = Path(self.temp.name) / "report.json"
        path.write_bytes(b' ' * (2 * 1024 * 1024 + 1))
        with self.assertRaises(RegistryError):
            read_json(path)

    def test_actual_json_reader_refuses_symlink_file_and_ancestor(self):
        from ml.registry import RegistryError, read_json
        root = Path(self.temp.name)
        destination = root / "actual"
        destination.mkdir()
        (destination / "report.json").write_text('{"private":"do-not-read-through-link"}')
        (root / "linked.json").symlink_to(destination / "report.json")
        (root / "linked-dir").symlink_to(destination, target_is_directory=True)
        for path in (root / "linked.json", root / "linked-dir" / "report.json"):
            with self.subTest(path=path), self.assertRaises(RegistryError):
                read_json(path)

    def test_actual_json_reader_refuses_fifo_without_blocking(self):
        import os
        import subprocess
        import sys
        from django.conf import settings
        path = Path(self.temp.name) / "report.json"
        os.mkfifo(path)
        script = ('import sys\nfrom ml.registry import RegistryError, read_json\n'
                  'try:\n read_json(sys.argv[1])\nexcept RegistryError:\n sys.exit(0)\n'
                  'sys.exit(2)\n')
        result = subprocess.run([sys.executable, "-c", script, str(path)], cwd=settings.PROJECT_DIR,
                                timeout=5, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, "FIFO must fail promptly without opening as a regular JSON file")

    def test_failed_validation_remains_candidate_and_visible(self):
        value = status_fixture()
        value["models"][0].update(stage="candidate", evaluation_passed=False, simulation_passed=False)
        result = self.projected(value)
        model = result["models"][0]
        self.assertEqual(model["stage"], "candidate")
        self.assertIs(model["evaluation_passed"], False)
        self.assertIs(model["simulation_passed"], False)
        self.assertFalse(model["execution_authorized"])
        self.client.force_login(self.operator)
        with patch("common.ml_status._read_registry", return_value=value):
            response = self.client.get("/ai/")
        self.assertContains(response, "未通过")

    def test_result_flags_and_currency_cannot_be_coerced(self):
        value = status_fixture()
        value["models"][0]["simulation_passed"] = "true"
        self.assertFalse(self.projected(value)["available"])
        value = status_fixture()
        value["models"][0]["simulation_summary"] = {"net_pnl_units": 100, "pnl_mark_valid": True, "currency": "EUR"}
        self.assertFalse(self.projected(value)["available"])

    def test_explicit_null_invalid_valuation_is_visible_not_zero(self):
        value = status_fixture()
        value["models"][0]["simulation_summary"] = {"realized_pnl_units": 0, "unrealized_pnl_units": None, "fees_units": 0, "rebates_units": 0, "net_pnl_units": None, "drawdown_units": None, "max_drawdown_units": None, "pnl_mark_valid": False, "killed": False, "filled_quantity": 0}
        result = self.projected(value)
        self.assertTrue(result["available"])
        summary = result["models"][0]["simulation_summary"]
        self.assertIsNone(summary["net_pnl_units"])
        self.assertFalse(summary["ledger_complete"])
        value["models"][0]["simulation_summary"]["pnl_mark_valid"] = True
        self.assertFalse(self.projected(value)["available"])

    def test_actual_empty_store_is_read_only_and_not_trained(self):
        before = list(Path(self.temp.name).iterdir())
        result = get_ml_status()
        self.assertTrue(result["available"])
        self.assertEqual(result["state"], "empty")
        self.assertEqual(result["models"], [])
        self.assertEqual(list(Path(self.temp.name).iterdir()), before)

    def test_actual_registry_malformed_index_is_fail_closed(self):
        index = Path(self.temp.name) / "index.json"
        for raw in (b'{"schema_version":1,"models":[],"models":[]}', b'{"models":NaN}', b'{"broken":', b' ' * (128 * 1024 + 1)):
            index.write_bytes(raw)
            with self.subTest(bytes=len(raw)):
                result = get_ml_status()
                self.assertFalse(result["available"])
                self.assertEqual(result["state"], "invalid")
                self.assertEqual(result["models"], [])

    def test_actual_registry_traversal_and_excessive_count_are_rejected(self):
        index = Path(self.temp.name) / "index.json"
        cases = [[{"version": "../../private", "stage": "candidate"}],
                 [{"version": VERSION, "stage": "candidate"}] * 101]
        for models in cases:
            index.write_text(json.dumps({"schema_version": 1, "models": models}))
            with self.subTest(count=len(models)):
                self.assertEqual(get_ml_status()["state"], "invalid")

    def test_actual_registry_symlink_control_file_is_rejected(self):
        root = Path(self.temp.name)
        outside = root / "other.json"
        outside.write_text('{"schema_version":1,"models":[]}')
        index = root / "index.json"
        index.symlink_to(outside)
        self.assertEqual(get_ml_status()["state"], "invalid")
        index.unlink()
        index.symlink_to(root / "missing.json")
        self.assertEqual(get_ml_status()["state"], "invalid")

    def test_known_drift_trigger_uses_local_label_not_raw_reason(self):
        value = status_fixture()
        value["retrain"]["reason"] = "FEATURE_DRIFT"
        result = self.projected(value)
        self.assertEqual(result["retrain"]["trigger_label"], "检测到特征分布漂移")
        self.assertNotIn("reason", result["retrain"])

    def test_flat_closed_account_is_valued_without_restoring_market_or_approval(self):
        value = status_fixture()
        value["models"][0].update(simulation_summary=account_summary(), simulation_passed=False,
                                   simulation_failure_codes=["NO_FILLS"])
        result = self.projected(value)
        self.assertTrue(result["available"])
        model = result["models"][0]
        summary = model["simulation_summary"]
        self.assertEqual(summary["net_pnl_units"], 0)
        self.assertTrue(summary["ledger_complete"])
        self.assertTrue(summary["net_pnl_valid"])
        self.assertTrue(summary["drawdown_complete"])
        self.assertFalse(summary["quote_mark_valid"])
        self.assertFalse(summary["pnl_mark_valid"])
        self.assertFalse(model["simulation_passed"])
        self.assertFalse(model["execution_authorized"])
        self.assertNotIn("/private", json.dumps(result))
        self.assertNotIn("residual_inventory_by_session", summary)
        display = ml_display_context(result)
        self.assertEqual(display["models"][0]["simulation_money"]["net_pnl_units"], "0.0000")
        self.client.force_login(self.operator)
        with patch("common.ml_status._read_registry", return_value=value):
            page = self.client.get("/ai/")
        self.assertContains(page, "账户已平仓且无挂单")
        self.assertContains(page, "不会因此恢复报价权限")
        self.assertContains(page, "没有模拟成交，不能验证交易效果")
        self.assertContains(page, "零盈亏不能证明策略有效")
        self.assertContains(page, 'data-failure-code="NO_FILLS"')

    def test_terminal_flat_value_does_not_hide_an_incomplete_drawdown_path(self):
        value = status_fixture()
        value["models"][0].update(simulation_summary=account_summary(net=4200, realized=6000, fees=1800, fills=2, complete=False),
                                   simulation_passed=False, simulation_failure_codes=["INCOMPLETE_DRAWDOWN"])
        result = self.projected(value)
        self.assertTrue(result["available"])
        summary = result["models"][0]["simulation_summary"]
        self.assertEqual(summary["net_pnl_units"], 4200)
        self.assertIsNone(summary["max_drawdown_units"])
        self.assertFalse(summary["drawdown_complete"])
        self.assertTrue(summary["net_pnl_valid"])
        self.client.force_login(self.operator)
        with patch("common.ml_status._read_registry", return_value=value):
            page = self.client.get("/ai/")
        self.assertContains(page, "估值路径不完整，最大回撤不可核验")
        self.assertContains(page, 'data-metric="net-pnl">0.4200')
        self.assertContains(page, 'data-metric="drawdown">不可用')

    def test_account_valuation_refuses_mismatched_money_or_flags(self):
        cases = [
            ("net_pnl_units", 1), ("unrealized_pnl_units", 1), ("cash_before_fees_units", 1),
            ("pnl_mark_valid", True), ("open_orders_at_end", 1), ("open_exposure", True),
            ("fill_count", 1), ("filled_quantity", 1), ("fees_units", 1),
        ]
        for key, replacement in cases:
            value = status_fixture()
            summary = account_summary()
            summary[key] = replacement
            value["models"][0]["simulation_summary"] = summary
            with self.subTest(key=key):
                self.assertFalse(self.projected(value)["available"])

    def test_offsetting_session_positions_cannot_claim_flat_account(self):
        value = status_fixture()
        summary = account_summary()
        summary["residual_inventory_by_session"] = {"session-a": 1, "session-b": -1}
        value["models"][0]["simulation_summary"] = summary
        self.assertFalse(self.projected(value)["available"])
        summary["open_exposure"] = True
        self.assertFalse(self.projected(value)["available"])

    def test_new_valuation_requires_complete_exposure_and_money_evidence(self):
        for missing in ("cash_before_fees_units", "fees_units", "residual_inventory_by_session", "open_exposure", "fill_count"):
            value = status_fixture()
            summary = account_summary()
            del summary[missing]
            value["models"][0]["simulation_summary"] = summary
            with self.subTest(missing=missing):
                self.assertFalse(self.projected(value)["available"])
        for replacement in (None, {}, {"schema_version": 1}, {"schema_version": True}):
            value = status_fixture()
            value["models"][0]["simulation_summary"] = {**account_summary(), "valuation": replacement}
            with self.subTest(valuation=replacement):
                self.assertFalse(self.projected(value)["available"])

    def test_invalid_valuation_preserves_unavailable_values(self):
        value = status_fixture()
        summary = account_summary()
        summary.update(net_pnl_units=None, unrealized_pnl_units=None, max_drawdown_units=None,
                       inventory=1, open_exposure=True, fill_count=1, filled_quantity=1, fills=1,
                       cash_before_fees_units=-10000, residual_inventory_by_session={"session-a": 1})
        summary["valuation"].update(basis="unavailable", net_pnl_valid=False, drawdown_complete=False,
                                     net_pnl_units=None, unrealized_pnl_units=None, max_drawdown_units=None)
        value["models"][0]["simulation_summary"] = summary
        result = self.projected(value)
        self.assertTrue(result["available"])
        projected = result["models"][0]["simulation_summary"]
        self.assertIsNone(projected["net_pnl_units"])
        self.assertFalse(projected["ledger_complete"])
        self.assertTrue(projected["open_exposure"])
        summary["max_drawdown_units"] = summary["valuation"]["max_drawdown_units"] = 0
        self.assertFalse(self.projected(value)["available"])

    def test_mixed_valid_sessions_keep_terminal_value_and_open_exposure_separate(self):
        value = status_fixture()
        summary = account_summary(net=1000, fills=1)
        summary.update(unrealized_pnl_units=1000, inventory=1, open_exposure=True,
                       cash_before_fees_units=-10000,
                       residual_inventory_by_session={"holding-session": 1, "flat-session": 0})
        summary["valuation"].update(basis="mixed_valid", unrealized_pnl_units=1000)
        value["models"][0].update(simulation_summary=summary, simulation_passed=False,
                                   simulation_failure_codes=["OPEN_EXPOSURE"])
        result = self.projected(value)
        self.assertTrue(result["available"])
        ledger = result["models"][0]["simulation_summary"]
        self.assertEqual(ledger["net_pnl_units"], 1000)
        self.assertFalse(ledger["quote_mark_valid"])
        self.assertTrue(ledger["net_pnl_valid"])
        self.assertTrue(ledger["open_exposure"])
        self.assertFalse(result["models"][0]["simulation_passed"])
        summary["residual_inventory_by_session"] = {"holding-session": 1}
        self.assertFalse(self.projected(value)["available"])

    def test_fresh_quote_cannot_erase_an_earlier_valuation_gap(self):
        value = status_fixture()
        summary = account_summary(net=4200, realized=6000, fees=1800, fills=2, complete=False)
        summary["pnl_mark_valid"] = True
        summary["valuation"].update(basis="fresh_bbo", quote_mark_valid=True)
        value["models"][0]["simulation_summary"] = summary
        result = self.projected(value)
        self.assertTrue(result["available"])
        ledger = result["models"][0]["simulation_summary"]
        self.assertTrue(ledger["quote_mark_valid"])
        self.assertTrue(ledger["net_pnl_valid"])
        self.assertFalse(ledger["drawdown_complete"])
        self.assertIsNone(ledger["drawdown_units"])
        summary["cash_before_fees_units"] = 0
        self.assertFalse(self.projected(value)["available"])

    def test_failure_codes_use_local_labels_and_do_not_echo_unknown_diagnostics(self):
        value = status_fixture()
        value["models"][0].update(evaluation_passed=False, evaluation_failure_codes=["INVALID_EVIDENCE", "/private/SECRET"],
                                   simulation_passed=False, simulation_failure_codes=["NO_FILLS", "NO_FILLS"])
        result = self.projected(value)
        self.assertTrue(result["available"])
        model = result["models"][0]
        self.assertEqual(model["evaluation_failure_codes"], ["INVALID_EVIDENCE", "UNRECOGNIZED_REVIEW_REQUIRED"])
        self.assertEqual(model["simulation_failure_codes"], ["NO_FILLS"])
        self.assertNotIn("SECRET", json.dumps(result))
        value["models"][0]["evaluation_passed"] = True
        self.assertFalse(self.projected(value)["available"])

    def test_failure_code_types_and_length_are_bounded(self):
        for replacement in ("NO_FILLS", [1], ["NO_FILLS"] * 65, ["x" * 257]):
            value = status_fixture()
            value["models"][0]["simulation_failure_codes"] = replacement
            with self.subTest(replacement=type(replacement)):
                self.assertFalse(self.projected(value)["available"])
