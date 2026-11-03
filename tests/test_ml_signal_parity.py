"""Synthetic, offline-only event-sequence parity for the signal side filter."""
import csv
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from execution.ai_gate import AIParticipationGate
from ml.backtest import _signals
from ml.features import FeatureConfig, FeatureState
from ml.runtime import PolicyConfig, TradingPolicy


class DirectionalStub:
    """A constant forecast tests event handling, not learned market behaviour."""

    feature_config = FeatureConfig(window_events=4)
    horizon_ns = 500_000_000
    model_version = "synthetic-parity-stub"
    provenance = {"kind": "synthetic"}

    def to_dict(self):
        return {"purpose": "synthetic event parity only", "prediction_bps": 20.0}

    def predict(self, features):
        return 20.0


class SignalEventParityTests(unittest.TestCase):
    def setUp(self):
        self.start = 34_200_000_000_000
        self.config = PolicyConfig(signal_ttl_ns=50_000_000, min_expected_edge_bps=0.5,
                                   fee_per_share_units=0, min_fee_per_order_units=0,
                                   slippage_buffer_units=0, max_target_position=3,
                                   clock="exchange_logical")
        self.capture = SimpleNamespace(capture_id="synthetic-parity", symbol="TEST",
                                       itch_sha256="a" * 64, market_csv_sha256="b" * 64)
        self.rows = []
        for seq, offset, quantity in ((1, 0, 101), (2, 1, 102), (3, 2, 103),
                                      (4, 3, 104), (5, 3, 105), (6, 4, 106),
                                      (7, 5, 106), (8, 6, 107)):
            timestamp = self.start + offset * 1_000_000
            self.rows.append({"sequence": seq, "timestamp_ns": timestamp,
                              "logical_timestamp_ns": timestamp,
                              "bid": 999_900, "ask": 1_000_100,
                              "bid_qty": quantity, "ask_qty": 100,
                              "valid": True, "tradable": True})

    def generate(self, directory):
        with patch("ml.backtest.iter_market_rows", return_value=iter(self.rows)):
            paths, *_ = _signals(self.capture, DirectionalStub(), self.config, Path(directory))
        with paths["model"].open() as source:
            result = {int(row["ordinal"]): row for row in csv.DictReader(source)}
        structured = [json.loads(line) for line in
                      (Path(directory) / "synthetic-parity-signals.jsonl").read_text().splitlines()]
        return result, structured

    def test_changed_book_at_same_timestamp_explicitly_revokes_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            rows, structured = self.generate(directory)
        self.assertEqual(rows[4]["allow_bid"], "1")
        self.assertEqual((rows[5]["allow_bid"], rows[5]["allow_ask"]), ("0", "0"))
        self.assertEqual(int(rows[5]["expires_ns"]), self.rows[4]["timestamp_ns"])
        self.assertEqual(rows[6]["allow_bid"], "1")
        # Unchanged traffic preserves the original expiry instead of emitting anew.
        self.assertNotIn(7, rows)
        self.assertEqual(rows[8]["allow_bid"], "1")
        self.assertTrue(all(signal["execution_authorized"] is False for signal in structured))

    def online_gate(self):
        # Exercise only the state machine; a synthetic model cannot pass the real
        # constructor's approval checks and this object has no execution adapter.
        gate = object.__new__(AIParticipationGate)
        gate.symbol = "TEST"
        config = {**asdict(self.config), "clock": "monotonic"}
        gate.config = PolicyConfig(**config)
        gate.version, gate.artifact_sha256 = "synthetic-parity-stub", ""
        gate.features = FeatureState(DirectionalStub.feature_config)
        gate.policy = TradingPolicy(DirectionalStub(), gate.config)
        gate._last_market = gate._sample_market = None
        gate._latched_reason = ""
        gate._registry_check = lambda now: None
        gate.last_signal = gate._wait(0, 0, "NO_SIGNAL")
        return gate

    def test_offline_rows_match_online_side_filter_without_any_registry_or_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            rows, _ = self.generate(directory)
        gate = self.online_gate()
        online = {}
        for row in self.rows:
            now = 10_000_000_000 + row["timestamp_ns"] - self.start
            signal = gate.observe(sequence=row["sequence"], source_timestamp_ns=row["timestamp_ns"],
                                  market=row, now_ns=now, inventory=0, market_fresh=True)
            online[row["sequence"]] = signal
            if row["sequence"] in rows:
                expected = rows[row["sequence"]]
                self.assertEqual(signal["allow_bid"], expected["allow_bid"] == "1")
                self.assertEqual(signal["allow_ask"], expected["allow_ask"] == "1")
                self.assertIs(signal["execution_authorized"], False)
        self.assertEqual(online[7]["expires_ns"], online[6]["expires_ns"])
        self.assertTrue(online[7]["allow_bid"])

    def test_redundant_valid_status_checks_time_without_revoking_or_extending_ttl(self):
        gate = self.online_gate()
        previous = None
        for row in self.rows[:7]:
            now = 10_000_000_000 + row["timestamp_ns"] - self.start
            signal = gate.observe(sequence=row["sequence"], source_timestamp_ns=row["timestamp_ns"],
                                  market=row, now_ns=now, inventory=0, market_fresh=True,
                                  status_event=row["sequence"] == 7)
            if row["sequence"] == 6:
                previous = signal
        self.assertTrue(previous["allow_bid"])
        self.assertTrue(signal["allow_bid"])
        self.assertEqual(signal["expires_ns"], previous["expires_ns"])
        self.assertEqual(gate.features.last_sequence, 7)


if __name__ == "__main__":
    unittest.main()
