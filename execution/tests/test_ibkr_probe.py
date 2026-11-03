import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.ibkr_probe import (
    Configuration,
    ProbeError,
    Snapshot,
    load_sdk,
    main,
    write_private_snapshot,
)


class ConfigurationTests(unittest.TestCase):
    def test_paper_account_and_session_identity(self):
        configuration = Configuration(account="DU123456")
        configuration.validate()
        configuration.validate_managed_accounts("U999, DU123456,")
        with self.assertRaises(ProbeError):
            configuration.validate_managed_accounts("DU999999")

    def test_write_modes_refused(self):
        for mode in ("live", "paper", "write"):
            with self.assertRaisesRegex(ProbeError, "read-only"):
                Configuration(account="DU123456", mode=mode).validate()

    def test_live_readonly_is_explicit_and_cannot_attest_write_mode(self):
        Configuration(account="U123456", account_kind="live").validate()
        self.assertFalse(Snapshot("U123456").as_dict()["trading_enabled"])
        self.assertFalse(Snapshot("U123456").as_dict()["account_mode_attested"])

    def test_client_zero_and_invalid_account_refused(self):
        for values in ({"client_id": 0}, {"account": ""}, {"account": "U123456"},
                       {"port": 70000}, {"timeout": 0}):
            arguments = {"account": "DU123456", **values}
            with self.assertRaises(ProbeError):
                Configuration(**arguments).validate()


class SnapshotTests(unittest.TestCase):
    def test_exact_execution_duplicate_does_not_double_count(self):
        snapshot = Snapshot("DU123456")
        execution = {"execution_id": "0001.01", "quantity": "5"}
        snapshot.put("executions", "0001.01", execution)
        snapshot.put("executions", "0001.01", execution)
        self.assertEqual(len(snapshot.executions), 1)

    def test_conflicting_duplicate_refused(self):
        snapshot = Snapshot("DU123456")
        snapshot.put("executions", "0001.01", {"quantity": "5"})
        with self.assertRaisesRegex(ProbeError, "Conflicting"):
            snapshot.put("executions", "0001.01", {"quantity": "7"})

    def test_correction_identity_is_retained_not_treated_as_duplicate(self):
        snapshot = Snapshot("DU123456")
        snapshot.put("executions", "0001.01", {"quantity": "5"})
        snapshot.put("executions", "0001.02", {"quantity": "4"})
        self.assertEqual(len(snapshot.executions), 2)
        self.assertFalse(snapshot.as_dict()["ledger_reconciled"])

    def test_capacity_failure_explicit(self):
        snapshot = Snapshot("DU123456")
        snapshot.MAX_RECORDS = 1
        snapshot.put("positions", "1", {"quantity": "1"})
        with self.assertRaisesRegex(ProbeError, "capacity"):
            snapshot.put("positions", "2", {"quantity": "2"})

    def test_output_private_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            write_private_snapshot(output, {"trading_enabled": False})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertFalse(json.loads(output.read_text())["trading_enabled"])
            with self.assertRaises(FileExistsError):
                write_private_snapshot(output, {})

    def test_output_symlink_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "target"
            target.write_text("keep")
            output = Path(temporary) / "snapshot.json"
            output.symlink_to(target)
            with self.assertRaises(FileExistsError):
                write_private_snapshot(output, {})
            self.assertEqual(target.read_text(), "keep")


class AvailabilityTests(unittest.TestCase):
    @patch("execution.ibkr_probe.importlib.import_module", side_effect=ImportError("absent"))
    def test_missing_sdk_fail_closed(self, _):
        with self.assertRaisesRegex(ProbeError, "SDK unavailable"):
            load_sdk()

    def test_invalid_config_cannot_create_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            with patch.dict(os.environ, {"IBKR_EXECUTION_MODE": "live"}):
                with patch("sys.stderr"):
                    self.assertEqual(main(["--account", "DU123456", "--output", str(output)]), 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
