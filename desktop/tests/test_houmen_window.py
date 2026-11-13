import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from vector_desktop.houmen_window import HoumenWindow, read_evidence


class HoumenDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        body = {"fixture": True}
        version = "houmen-ridge-" + hashlib.sha256(b'{"fixture":true}').hexdigest()[:24]
        (self.path / "model.json").write_text(json.dumps({**body, "model_version": version}))
        self.report = {"model_version": version, "development_samples": 10, "holdout_samples": 3,
                       "research_checks_passed": False,
                       "holdout": {"ridge": {"rmse_bps": 12}, "zero_return": {"rmse_bps": 10}},
                       "backtests": {name: {"net_return": 0, "max_drawdown": 0, "completed_round_trips": 0}
                                     for name in ("houmen_rules", "houmen_ai")}}
        self.file = self.path / "training-report.json"
        self.file.write_text(json.dumps(self.report))
        self.window = HoumenWindow()

    def tearDown(self):
        self.window.close()
        self.temp.cleanup()

    def test_evidence_load_does_not_grant_trading_permission(self):
        self.window.load_report(self.file)
        self.assertEqual(self.window.table.rowCount(), 11)
        self.assertIn("未通过", self.window.report_status.text())

    def test_tampered_model_rejected(self):
        (self.path / "model.json").write_text('{"model_version":"wrong"}')
        with self.assertRaises(ValueError):
            read_evidence(self.file)

    def test_failed_reload_clears_old_results(self):
        self.window.load_report(self.file)
        self.file.write_text("not JSON")
        self.window.load_report(self.file)
        self.assertEqual(self.window.table.rowCount(), 0)
        self.assertIn("无法读取", self.window.report_status.text())

    def test_report_version_must_match_model(self):
        self.report["model_version"] = "another"
        self.file.write_text(json.dumps(self.report))
        with self.assertRaises(ValueError):
            read_evidence(self.file)

    def test_empty_credentials_never_start_request(self):
        with patch("execution.alpaca_cloud.AlpacaClient", side_effect=AssertionError):
            self.window.check_cloud()
        self.assertFalse(self.window.busy)
        self.assertIn("请填写", self.window.connection.text())

    def test_close_clears_credentials(self):
        self.window.key.setText("fixture-key")
        self.window.secret.setText("fixture-secret")
        self.window.close()
        self.assertEqual(self.window.secret.text(), "")
        self.assertEqual(self.window.key.text(), "")
