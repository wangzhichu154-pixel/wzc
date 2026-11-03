import json
from pathlib import Path
import tempfile
import time

from django.test import TestCase, override_settings
from common.execution_status import get_paper_status


class ExecutionStatusTests(TestCase):
    @override_settings(PAPER_STATUS_PATH="")
    def test_unconfigured_is_unavailable(self):
        self.assertFalse(get_paper_status()["available"])

    def test_fresh_allowlisted_and_stale_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            data = {"schema_version": 1, "kind": "ibkr-paper", "updated_at_ns": time.time_ns(),
                    "state": "read_only", "connected": False, "armed": False, "pnl_mark_valid": False,
                    "kill_latched": False, "inventory": 0, "open_orders": 0, "net_pnl_units": 99,
                    "account_id": "must-not-leak"}
            path.write_text(json.dumps(data))
            with override_settings(PAPER_STATUS_PATH=str(path)):
                result = get_paper_status()
                self.assertTrue(result["available"])
                self.assertNotIn("account_id", result)
                self.assertIsNone(result["net_pnl_units"])
                data["updated_at_ns"] -= 100_000_000_000
                path.write_text(json.dumps(data))
                self.assertEqual(get_paper_status()["state"], "stale")

    def test_anonymous_cannot_read_execution_status(self):
        self.assertEqual(self.client.get("/api/v1/execution/status/").status_code, 401)
