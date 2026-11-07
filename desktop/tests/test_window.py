"""Native widget behavior tests. Fake service responses exist only in tests."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication
from vector_desktop.client import ApiError
from vector_desktop.window import MainWindow, apply_palette
from vector_desktop.widgets import CollectionPage, CreateRunDialog, SimulationForm, job_summary, text


APP = QApplication.instance() or QApplication([])
apply_palette(APP)


def drain(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError("Qt operation timed out")
        time.sleep(.002)
    APP.processEvents()


def risk_values():
    return {"max_order_size": 100, "tick_size": 100, "soft_inventory_limit": 500,
            "hard_inventory_limit": 1000, "orders_per_second": 20,
            "daily_loss_limit": "1000.00", "persisted": True}


class ServiceFixture:
    def __init__(self):
        self.calls = []
        self.login_event = None
        self.logout_failure = False

    def health(self):
        self.calls.append(("health", threading.get_ident()))
        return {"ready": True}

    def login(self, email, password):
        self.calls.append(("login", threading.get_ident()))
        if self.login_event:
            self.login_event.wait(2)
        if password != "test-password-only":
            raise ApiError("邮箱或密码无效。", code="INVALID_CREDENTIALS", status=401)
        return {"email": email, "is_operator": True, "email_verified": True}

    def logout(self):
        if self.logout_failure:
            raise ApiError("连接失败", code="CONNECTION_FAILED")

    def dashboard(self):
        return {"dataset_count": 2, "job_count": 4, "running_count": 0,
                "completed_count": 3, "automation_running": 0,
                "automation_attention": 0, "research_paused": False,
                "live_trading": False}

    def datasets(self, page=1):
        return {"items": [], "page": page, "pages": 1, "total": 0}

    jobs = datasets
    automations = datasets

    def risk(self):
        return risk_values()

    def ai_status(self):
        return {"available": False, "state": "not_configured", "models": []}

    def execution_status(self):
        return {"available": False, "state": "stale", "live_enabled": False}

    def devices(self):
        return {"devices": []}


class NativeWindowTests(unittest.TestCase):
    def setUp(self):
        self.client = ServiceFixture()
        self.window = MainWindow(self.client, auto_check=False)
        self.window.show()
        APP.processEvents()
        self.warning_patch = patch("vector_desktop.window.warning", return_value=None)
        self.warning_patch.start()

    def tearDown(self):
        if self.client.login_event:
            self.client.login_event.set()
        drain(lambda: not self.window.busy)
        self.window.close()
        APP.processEvents()
        self.warning_patch.stop()

    def login(self):
        self.window.email.setText("ui-test@example.test")
        self.window.password.setText("test-password-only")
        self.window.login()
        drain(lambda: not self.window.busy)
        self.window.timer.stop()

    def test_login_runs_off_gui_thread_and_clears_password_before_request(self):
        self.client.login_event = threading.Event()
        self.window.email.setText("ui-test@example.test")
        self.window.password.setText("test-password-only")
        self.window.login()
        self.assertEqual(self.window.password.text(), "")
        self.assertTrue(self.window.busy)
        drain(lambda: bool(self.client.calls))
        self.assertNotEqual(self.client.calls[0][1], threading.get_ident())
        self.assertFalse(self.window.submit(self.client.health, lambda _: None))
        self.client.login_event.set()
        drain(lambda: not self.window.busy)
        self.assertIs(self.window.stack.currentWidget(), self.window.workspace)
        self.assertEqual(self.window.metrics["job_count"].text(), "4")

    def test_login_failure_keeps_login_page_and_does_not_save_password(self):
        self.window.email.setText("ui-test@example.test")
        self.window.password.setText("wrong-test-password")
        self.window.login()
        drain(lambda: not self.window.busy)
        self.assertIsNone(self.window.user)
        self.assertEqual(self.window.password.text(), "")
        self.assertIn("无效", self.window.login_message.text())

    def test_logout_transport_failure_still_clears_private_workspace(self):
        self.login()
        self.client.logout_failure = True
        self.window.logout()
        drain(lambda: not self.window.busy)
        self.assertIsNone(self.window.user)
        self.assertIs(self.window.stack.currentWidget(), self.window.login_page)
        self.assertEqual(self.window.metrics["job_count"].text(), "—")
        self.assertFalse(self.window.risk_loaded)
        self.assertIn("服务端退出未确认", self.window.login_message.text())

    def test_unauthorized_response_resets_workspace(self):
        self.login()
        def expired():
            raise ApiError("登录失效", status=401)
        self.window.submit(expired, lambda _: None)
        drain(lambda: not self.window.busy)
        self.assertIsNone(self.window.user)
        self.assertEqual(self.window.password.text(), "")

    def test_full_risk_range_preserves_large_backend_limit(self):
        values = risk_values()
        values["daily_loss_limit"] = "9876543210.12"
        self.window._render_risk(values)
        self.assertEqual(f"{self.window.risk_fields['daily_loss_limit'].value():.2f}", "9876543210.12")
        self.assertTrue(self.window.risk_loaded)

    def test_invalid_risk_payload_disables_save_without_partial_write(self):
        self.window._render_risk(risk_values())
        values = risk_values()
        values["max_order_size"] = 200
        values["daily_loss_limit"] = "NaN"
        with self.assertRaises(ValueError):
            self.window._render_risk(values)
        self.assertFalse(self.window.risk_loaded)
        self.assertFalse(self.window.risk_save.isEnabled())
        self.assertEqual(self.window.risk_fields["max_order_size"].value(), 100)

    def test_unavailable_execution_does_not_display_fake_zero_inventory(self):
        self.window._render_execution({"available": False, "state": "stale", "inventory": 0})
        self.assertEqual(self.window.execution_values["inventory"].text(), "不可用")
        self.assertIn("过期", self.window.execution_status.text())

    def test_empty_model_store_is_explicit(self):
        self.window._render_ai({"available": False, "state": "not_configured", "models": []})
        self.assertEqual(self.window.ai_page.table.rowCount(), 0)
        self.assertIn("尚未配置", self.window.ai_page.empty.text())

    def test_native_pages_connect_real_client_methods(self):
        self.login()
        for row in range(1, 8):
            self.window.navigation.setCurrentRow(row)
            drain(lambda: not self.window.busy)
            self.assertEqual(self.window.pages.currentIndex(), row)
        self.assertTrue(self.window.risk_loaded)

    def test_close_waits_for_inflight_request_without_blocking_gui(self):
        release = threading.Event()
        self.window.submit(lambda: release.wait(2), lambda _: None)
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window.closing)
        release.set()
        drain(lambda: not self.window.busy)
        self.assertFalse(self.window.isVisible())

    def test_optional_screenshots_are_explicit_test_fixtures(self):
        destination = os.environ.get("VECTOR_DESKTOP_SCREENSHOTS")
        if not destination:
            self.skipTest("Screenshot destination not requested")
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self.window.grab().save(str(directory / "native-login.png")))
        self.login()
        self.window.identity.setText("界面自动化测试 · 以下数量为测试夹具数据")
        APP.processEvents()
        self.assertTrue(self.window.grab().save(str(directory / "native-overview-test-fixture.png")))


class NativeWidgetTests(unittest.TestCase):
    def test_partial_bad_list_clears_model_and_visual_rows_together(self):
        page = CollectionPage("测试", "", [("编号", lambda row: row["id"]), ("长度", lambda row: row["size"] / 10)])
        page.render({"items": [{"id": "old", "size": 10}]})
        page.table.setCurrentCell(0, 0)
        with self.assertRaises(KeyError):
            page.render({"items": [{"id": "new", "size": 20}, {"id": "bad"}]})
        self.assertEqual(page.items, [])
        self.assertEqual(page.table.rowCount(), 0)
        self.assertIsNone(page.selected())

    def test_refresh_clears_selection_instead_of_applying_action_to_different_row(self):
        page = CollectionPage("测试", "", [("编号", lambda row: row["id"])])
        page.render({"items": [{"id": "old"}]})
        page.table.setCurrentCell(0, 0)
        page.render({"items": [{"id": "new"}]})
        self.assertIsNone(page.selected())

    def test_modes_never_offer_live_and_costs_are_exact_integer_units(self):
        dialog = CreateRunDialog({"name": "测试行情"})
        self.assertEqual([dialog.mode.itemData(i) for i in range(dialog.mode.count())], ["replay", "simulation"])
        values = SimulationForm().values()
        self.assertEqual(values["latency_ns"], 1000000)
        self.assertEqual(values["fee_per_share_units"], 30)
        self.assertEqual(values["rebate_per_share_units"], 0)

    def test_automation_requires_name_and_starts_in_stopped_state(self):
        dialog = CreateRunDialog({"name": "测试行情"}, automation=True)
        dialog.validate()
        self.assertNotEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertEqual(dialog.mode.currentData(), "simulation")
        self.assertFalse(dialog.mode.isEnabled())

    def test_missing_financial_evidence_is_not_zero(self):
        data = {"mode": "simulation", "status": "succeeded", "result": {"summary": {"simulation": {
            "net_pnl_units": 12345, "pnl_mark_valid": False, "valuation": {"net_pnl_valid": False,
            "net_pnl_units": None, "drawdown_complete": False, "max_drawdown_units": None}}}}}
        rows = dict(job_summary(data))
        self.assertEqual(rows["模拟净盈亏（USD）"], "估值不可用")
        self.assertEqual(rows["最大模拟回撤（USD）"], "估值不可用")
        self.assertEqual(text(None), "未记录")

    def test_valid_flat_ledger_and_unknown_drawdown_are_separate(self):
        data = {"mode": "simulation", "status": "succeeded", "result": {"summary": {"simulation": {
            "pnl_mark_valid": False, "valuation": {"net_pnl_valid": True, "net_pnl_units": 0,
            "drawdown_complete": False, "max_drawdown_units": None}}}}}
        rows = dict(job_summary(data))
        self.assertEqual(rows["模拟净盈亏（USD）"], "0.0000")
        self.assertEqual(rows["最大模拟回撤（USD）"], "估值不可用")


if __name__ == "__main__":
    unittest.main()
