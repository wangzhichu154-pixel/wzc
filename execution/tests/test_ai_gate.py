"""Offline adapter tests. Registry approval is mocked only in this test file.

The licensed-labelled numerical fixture is fabricated engineering input. It has
no real provenance/approval and cannot pass the production registry loader.
"""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution.ai_gate import AIGateError, AIParticipationGate
from ml.features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FeatureConfig, REGULAR_OPEN_NS
from ml.model import artifact_version
from ml.registry import ApprovedModel
from ml.runtime import PolicyConfig


def fixture_gate(directory, config=None, prediction=500.0, kind="licensed_market", mutate=None):
    config = config or PolicyConfig(fee_per_share_units=0, min_fee_per_order_units=1,
                                   slippage_buffer_units=0, max_target_position=10)
    payload = {
        "schema_version": 1, "model_type": "ridge_mid_return_bps",
        "features": {"schema_version": FEATURE_SCHEMA_VERSION, "names": list(FEATURE_NAMES),
                     "config": asdict(FeatureConfig(window_events=4))},
        "preprocessing": {"mean": [0.0] * 6, "scale": [1.0] * 6},
        "estimator": {"coefficients": [0.0] * 6, "intercept": prediction, "alpha": 1.0},
        "target": {"name": "future_mid_return_bps", "horizon_ns": 500_000_000,
                   "max_label_delay_ns": 100_000_000, "max_abs_prediction_bps": 1000.0},
        "provenance": {"kind": kind, "symbol": "TEST", "source": "FABRICATED OFFLINE TEST ONLY"},
        "training": {"purpose": "Engineering regression, no market evidence or approval"},
    }
    payload["model_version"] = artifact_version(payload)
    raw = json.dumps(payload).encode()
    path = Path(directory) / "test-only-model.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    manifest = {"schema_version": 1, "version": payload["model_version"], "model_sha256": digest,
                "provenance_kind": kind, "authorized_use_attested": True, "symbol": "TEST",
                "target_horizon_ns": 500_000_000, "policy": config.policy_document(),
                "policy_sha256": config.sha256,
                "policy_clock_contract": "paper:monotonic;simulation:exchange_logical"}
    approved = ApprovedModel(path, payload["model_version"], digest, manifest)
    if mutate:
        mutate(approved)
    with patch("ml.registry.load_approved_model", return_value=approved) as loader:
        gate = AIParticipationGate(Path(directory), "TEST", config)
    return gate, approved, loader


def warmup(gate, now, *, bid=10000, ask=10200, inventory=0):
    result = None
    for index in range(4):
        market = {"bid": bid, "ask": ask, "bid_qty": 97 + index, "ask_qty": 100,
                  "valid": True, "tradable": True}
        result = gate.observe(sequence=index + 1, source_timestamp_ns=REGULAR_OPEN_NS + index * 1_000_000,
                              market=market, now_ns=now + index, inventory=inventory, market_fresh=True)
    return result, market


class ApprovedAIGateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 10_000_000_000

    def gate(self, **kwargs):
        return fixture_gate(self.directory.name, **kwargs)

    def current(self, gate, market, now=None, inventory=0):
        return gate.current(now_ns=now or self.now + 10, inventory=inventory,
                            market=market, market_fresh=True)

    def assert_wait(self, result):
        self.assertEqual(result["action"], "WAIT")
        self.assertFalse(result["allow_bid"])
        self.assertFalse(result["allow_ask"])
        self.assertFalse(result["execution_authorized"])

    def test_default_loader_rejects_unregistered_fixture(self):
        gate, _, _ = self.gate()
        with self.assertRaises(ValueError):
            AIParticipationGate(Path(self.directory.name), "TEST", gate.config)

    def test_synthetic_and_policy_mismatch_are_refused(self):
        with self.assertRaises(AIGateError):
            self.gate(kind="synthetic")
        with self.assertRaises(AIGateError):
            self.gate(mutate=lambda a: a.manifest["policy"].update(max_target_position=100))
        with self.assertRaises(AIGateError):
            self.gate(mutate=lambda a: a.manifest.update(symbol="OTHER"))

    def test_exact_file_hash_and_symlink_are_refused(self):
        with self.assertRaises(AIGateError):
            self.gate(mutate=lambda a: a.path.write_text(a.path.read_text() + " "))
        def symlink(approved):
            real = approved.path.with_suffix(".real")
            approved.path.rename(real)
            approved.path.symlink_to(real)
        with self.assertRaises(OSError):
            self.gate(mutate=symlink)

    def test_clock_and_short_permission_cannot_be_overridden(self):
        for config in (PolicyConfig(clock="exchange_logical"), PolicyConfig(allow_short=True)):
            with self.subTest(config=config), self.assertRaises(AIGateError):
                self.gate(config=config)

    def test_causal_warmup_and_unmodified_quote_contract(self):
        gate, approved, _ = self.gate()
        result, _ = warmup(gate, self.now)
        self.assertTrue(result["allow_bid"])
        self.assertFalse(result["allow_ask"])
        self.assertEqual(result["source_timestamp_ns"], REGULAR_OPEN_NS + 3_000_000)
        self.assertEqual(result["artifact_file_sha256"], approved.sha256)
        self.assertEqual(len(gate.features.history), 4)
        self.assertNotIn("quantity", result)
        self.assertNotIn("price", result)

    def test_unchanged_messages_cannot_renew_ttl(self):
        gate, _, _ = self.gate()
        signal, market = warmup(gate, self.now)
        first = signal["expires_ns"]
        duplicate = gate.observe(sequence=99, source_timestamp_ns=REGULAR_OPEN_NS + 9_000_000,
                                 market=market, now_ns=first - 1, inventory=0, market_fresh=True)
        self.assertEqual(duplicate["expires_ns"], first)
        self.assertTrue(duplicate["allow_bid"])
        self.assert_wait(self.current(gate, market, first))

    def test_same_timestamp_change_invalidates_previous_feature(self):
        gate, _, _ = self.gate()
        signal, market = warmup(gate, self.now)
        self.assertTrue(signal["allow_bid"])
        changed = {**market, "ask_qty": 99}
        result = gate.observe(sequence=5, source_timestamp_ns=REGULAR_OPEN_NS + 3_000_000,
                              market=changed, now_ns=self.now + 4, inventory=0, market_fresh=True)
        self.assert_wait(result)
        self.assert_wait(self.current(gate, changed))

    def test_cost_not_covered_and_short_signal_wait(self):
        for prediction in (0.1, -500.0):
            with self.subTest(prediction=prediction):
                gate, _, _ = self.gate(prediction=prediction)
                signal, _ = warmup(gate, self.now)
                self.assert_wait(signal)

    def test_no_lookahead_from_monotonic_time(self):
        gate, _, _ = self.gate()
        market = {"bid": 10000, "ask": 10200, "bid_qty": 100, "ask_qty": 100,
                  "valid": True, "tradable": True}
        signal = gate.observe(sequence=1, source_timestamp_ns=REGULAR_OPEN_NS - 1,
                              market=market, now_ns=10**18, inventory=0, market_fresh=True)
        self.assert_wait(signal)
        self.assertEqual(len(gate.features.history), 0)

    def test_changed_approval_latches_wait_and_cannot_auto_replace(self):
        gate, approved, loader = self.gate()
        _, market = warmup(gate, self.now)
        loader.return_value = replace(approved, sha256="0" * 64)
        self.assert_wait(self.current(gate, market, self.now + 1_000_000_000))
        self.assertEqual(gate.last_signal["reason_codes"], ["MODEL_REVOKED_OR_CHANGED"])
        loader.return_value = approved
        self.assert_wait(self.current(gate, market, self.now + 1_000_000_001))

    def test_feature_failure_latches_wait_even_after_good_input(self):
        gate, _, _ = self.gate()
        _, market = warmup(gate, self.now)
        result = gate.observe(sequence=5, source_timestamp_ns=REGULAR_OPEN_NS,
                              market={**market, "bid_qty": 101}, now_ns=self.now + 5,
                              inventory=0, market_fresh=True)
        self.assert_wait(result)
        self.assertEqual(result["reason_codes"], ["MODEL_OR_FEATURE_FAILURE"])
        self.assert_wait(self.current(gate, market))

    def test_unchanged_status_event_still_checks_source_clock(self):
        gate, _, _ = self.gate()
        _, market = warmup(gate, self.now)
        result = gate.observe(sequence=5, source_timestamp_ns=REGULAR_OPEN_NS,
                              market=market, now_ns=self.now + 5, inventory=0,
                              market_fresh=True, status_event=True)
        self.assert_wait(result)
        self.assertEqual(result["reason_codes"], ["MODEL_OR_FEATURE_FAILURE"])

    def test_redundant_valid_status_preserves_signal_without_extending_ttl(self):
        gate, _, _ = self.gate()
        signal, market = warmup(gate, self.now)
        result = gate.observe(sequence=5, source_timestamp_ns=REGULAR_OPEN_NS + 4_000_000,
                              market=market, now_ns=self.now + 5, inventory=0,
                              market_fresh=True, status_event=True)
        self.assertTrue(result["allow_bid"])
        self.assertEqual(result["expires_ns"], signal["expires_ns"])
