"""Offline policy safety regressions; synthetic coefficients are not market evidence."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ml.features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, FeatureConfig, FeatureSample
from ml.model import LinearModel, artifact_version, canonical_bytes
from ml.runtime import PolicyConfig, TradingPolicy


def fixture_model(prediction=20.0, *, maximum_prediction=1000.0):
    payload = {
        "schema_version": 1,
        "model_type": "ridge_mid_return_bps",
        "features": {"schema_version": FEATURE_SCHEMA_VERSION,
                     "names": list(FEATURE_NAMES), "config": asdict(FeatureConfig())},
        "preprocessing": {"mean": [0.0] * len(FEATURE_NAMES),
                          "scale": [1.0] * len(FEATURE_NAMES)},
        "estimator": {"coefficients": [0.0] * len(FEATURE_NAMES),
                      "intercept": prediction, "alpha": 1.0},
        "target": {"name": "future_mid_return_bps", "horizon_ns": 500_000_000,
                   "max_label_delay_ns": 100_000_000,
                   "max_abs_prediction_bps": maximum_prediction},
        "provenance": {"kind": "synthetic", "source": "policy unit test"},
        "training": {"purpose": "synthetic engineering test only"},
    }
    payload["model_version"] = artifact_version(payload)
    return LinearModel(payload)


class RuntimeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000_000
        self.market = {"bid": 999_900, "ask": 1_000_100,
                       "bid_qty": 100, "ask_qty": 100, "valid": True, "tradable": True}
        self.sample = FeatureSample(100, 34_200_000_000_000, 1_000_000.0,
                                    (2.0, 0.0, 0.0, 0.0, 0.0, 5.0))
        self.config = PolicyConfig(signal_ttl_ns=50_000_000, min_expected_edge_bps=0.5,
                                   fee_per_share_units=0, min_fee_per_order_units=0,
                                   slippage_buffer_units=0, max_target_position=3)

    def policy(self, prediction=20.0, **config):
        return TradingPolicy(fixture_model(prediction), replace(self.config, **config))

    def evaluate(self, policy, *, sample=None, market=None, inventory=0, now=None, symbol="TEST"):
        return policy.evaluate(self.sample if sample is None else sample, symbol=symbol,
                               now_ns=self.now if now is None else now,
                               market=self.market if market is None else market, inventory=inventory)

    def assert_wait(self, result):
        self.assertFalse(result["allow_bid"])
        self.assertFalse(result["allow_ask"])
        self.assertEqual(result["action"], "WAIT")
        self.assertIs(result["execution_authorized"], False)

    def test_prediction_only_filters_sides_and_never_authorizes_execution(self):
        result = self.evaluate(self.policy())
        self.assertTrue(result["allow_bid"])
        self.assertFalse(result["allow_ask"])
        self.assertEqual(result["target_position"], 3)
        self.assertIs(result["execution_authorized"], False)
        self.assertNotIn("order_id", result)
        self.assertNotIn("quantity", result)

    def test_duplicate_source_must_not_bypass_invalid_book_or_halt(self):
        for change in ({"valid": False}, {"tradable": False}, {"bid_qty": 0}):
            with self.subTest(change=change):
                policy = self.policy()
                self.assertTrue(self.evaluate(policy)["allow_bid"])
                self.assert_wait(self.evaluate(policy, market={**self.market, **change}, now=self.now + 1))
                # A subsequent duplicate cannot resurrect the permission just revoked.
                self.assert_wait(self.evaluate(policy, now=self.now + 2))

    def test_duplicate_source_cannot_reuse_features_for_another_midpoint(self):
        policy = self.policy()
        self.evaluate(policy)
        self.assert_wait(self.evaluate(policy, now=self.now + 1,
                                       market={**self.market, "bid": 1_000_000}))

    def test_duplicate_source_cannot_reuse_permission_after_same_mid_spread_widens(self):
        policy = self.policy()
        self.assertTrue(self.evaluate(policy)["allow_bid"])
        # Forecast is 20 bps; this new spread alone costs 40 bps, with the same midpoint.
        widened = {**self.market, "bid": 998_000, "ask": 1_002_000}
        self.assert_wait(self.evaluate(policy, market=widened, now=self.now + 1))

    def test_duplicate_source_is_bound_to_one_symbol(self):
        policy = self.policy()
        self.evaluate(policy)
        self.assert_wait(self.evaluate(policy, symbol="OTHER", now=self.now + 1))

    def test_duplicate_does_not_extend_ttl_and_expiry_is_inclusive(self):
        policy = self.policy()
        original = self.evaluate(policy)
        expiry = original["expires_ns"]
        duplicate = self.evaluate(policy, now=expiry - 1)
        self.assertTrue(duplicate["allow_bid"])
        self.assertEqual(duplicate["expires_ns"], expiry)
        self.assertEqual(duplicate["signal_time_ns"], original["signal_time_ns"])
        self.assert_wait(self.evaluate(policy, now=expiry))
        self.assert_wait(policy.current(now_ns=expiry + 1, inventory=0))

    def test_feature_reset_revokes_cached_signal(self):
        policy = self.policy()
        self.evaluate(policy)
        self.assert_wait(policy.evaluate(None, symbol="TEST", now_ns=self.now + 1,
                                         market=self.market, inventory=0))
        self.assert_wait(policy.current(now_ns=self.now + 2, inventory=0))

    def test_stale_market_and_regressed_source_or_clock_fail_closed(self):
        for failure in ("stale", "source", "clock"):
            with self.subTest(failure=failure):
                policy = self.policy()
                self.evaluate(policy)
                if failure == "stale":
                    result = policy.evaluate(self.sample, symbol="TEST", now_ns=self.now + 1,
                                             market=self.market, inventory=0, market_fresh=False)
                elif failure == "source":
                    result = self.evaluate(policy, sample=replace(self.sample, sequence=99), now=self.now + 1)
                else:
                    result = self.evaluate(policy, now=self.now - 1)
                self.assert_wait(result)

    def test_insufficient_edge_including_one_share_minimum_fee_denies_trade(self):
        self.assert_wait(self.evaluate(self.policy(prediction=2.0)))
        # A $1 per-order minimum on a one-share partial fill dominates a 20 bps forecast.
        self.assert_wait(self.evaluate(self.policy(min_fee_per_order_units=10_000)))

    def test_nan_features_and_out_of_range_prediction_fail_closed(self):
        bad_sample = replace(self.sample, features=(float("nan"),) + self.sample.features[1:])
        self.assert_wait(self.evaluate(self.policy(), sample=bad_sample))
        outlier = TradingPolicy(fixture_model(200.0, maximum_prediction=100.0), self.config)
        self.assert_wait(self.evaluate(outlier))

    def test_nonfinite_model_return_cannot_open_a_side(self):
        class NonfiniteEstimator:
            horizon_ns = 500_000_000
            model_version = "synthetic-nonfinite-test"
            provenance = {"kind": "synthetic"}

            def to_dict(self):
                return {"purpose": "synthetic nonfinite estimator test"}

            def predict(self, features):
                return float("nan")

        self.assert_wait(self.evaluate(TradingPolicy(NonfiniteEstimator(), self.config)))

    def test_current_inventory_cap_overrides_cached_buy_permission(self):
        policy = self.policy()
        self.assertTrue(self.evaluate(policy, inventory=2)["allow_bid"])
        self.assert_wait(policy.current(now_ns=self.now + 1, inventory=3))

    def test_long_only_sell_requires_actual_inventory(self):
        self.assert_wait(self.evaluate(self.policy(-20.0), inventory=0))
        policy = self.policy(-20.0)
        result = self.evaluate(policy, inventory=1)
        self.assertTrue(result["allow_ask"])
        self.assertFalse(result["execution_authorized"])
        self.assert_wait(policy.current(now_ns=self.now + 1, inventory=0))

    def test_monotonic_runtime_cannot_defer_inventory_to_offline_simulator(self):
        policy = self.policy(-20.0)
        with self.assertRaises(ValueError):
            policy.research_participation(self.sample, symbol="TEST", now_ns=self.now, market=self.market)
        with self.assertRaises(ValueError):
            policy.evaluate(self.sample, symbol="TEST", now_ns=self.now, market=self.market,
                            inventory=0, _defer_inventory=True)

    def test_offline_direction_can_request_sell_but_never_claim_inventory_authority(self):
        policy = self.policy(-20.0, clock="exchange_logical")
        result = policy.research_participation(self.sample, symbol="TEST", now_ns=self.now, market=self.market)
        self.assertTrue(result["allow_ask"])
        self.assertIs(result["inventory_check_deferred_to_simulator"], True)
        self.assertIs(result["execution_authorized"], False)
        # The ordinary inventory-aware API still cannot sell from a flat account.
        self.assert_wait(policy.current(now_ns=self.now + 1, inventory=0))

    def test_ttl_above_target_horizon_is_rejected(self):
        with self.assertRaises(ValueError):
            self.policy(signal_ttl_ns=600_000_000)

    def test_file_digest_and_model_content_digest_have_distinct_contracts(self):
        model = fixture_model()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-model.json"
            model.save(path)
            raw = path.read_bytes()
            loaded = LinearModel.load(path)
            result = self.evaluate(TradingPolicy(loaded, self.config))
            self.assertEqual(result["model_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(result["model_content_sha256"],
                             hashlib.sha256(canonical_bytes(json.loads(raw))).hexdigest())
            self.assertNotEqual(result["model_sha256"], result["model_content_sha256"])


if __name__ == "__main__":
    unittest.main()
