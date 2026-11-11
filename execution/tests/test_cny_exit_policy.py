from dataclasses import asdict, replace
from decimal import Decimal, localcontext
import unittest

from execution.cny_exit_policy import ExitFault, ExitRule, ExitState, Holding, Mark, OpenOrder, Phase, evaluate


class CnyExitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 10_000_000_000
        self.rule = ExitRule()
        self.state = ExitState("DU_TEST", 123)
        self.holding = Holding("DU_TEST", 123, 10000, (), self.now, True, True, True)
        self.stock = Mark(Decimal("100"), self.now, "123", "USD")
        # Deliberate arithmetic fixture, NOT a current/historical FX observation.
        self.fx = Mark(Decimal("5"), self.now, "USD", "CNY")

    def check(self, **changes):
        args = dict(rule=self.rule, state=self.state, holding=self.holding,
                    stock_bid=self.stock, usd_cny=self.fx, now_ns=self.now)
        return evaluate(**(args | changes))

    def test_exact_threshold_triggers_zero_target_without_send_authority(self):
        result = self.check()
        self.assertEqual(result.market_value_cny, Decimal("5000000"))
        self.assertEqual(result.phase, Phase.EXIT_WORKING)
        self.assertEqual(result.max_additional_sell_quantity, 10000)
        self.assertEqual(result.target_position, 0)
        self.assertTrue(result.state.triggered)
        self.assertTrue(result.block_new_buys)
        self.assertFalse(result.execution_authorized)

    def test_below_boundary_is_not_rounded_up_even_with_small_decimal_context(self):
        with localcontext() as context:
            context.prec = 3
            result = self.check(stock_bid=replace(self.stock, value=Decimal("99.9999998")))
        self.assertEqual(result.market_value_cny, Decimal("4999999.9900000"))
        self.assertEqual(result.phase, Phase.MONITOR)
        self.assertFalse(result.state.triggered)
        self.assertIsNone(result.target_position)

    def test_cny_is_not_cnh_or_an_inverse_rate(self):
        for mark in (replace(self.fx, quote="CNH"), replace(self.fx, base="CNY", quote="USD"), None):
            result = self.check(usd_cny=mark)
            self.assertEqual(result.phase, Phase.WAIT)
            self.assertTrue(result.block_new_buys)
            self.assertFalse(result.state.triggered)

    def test_marks_have_independent_source_freshness(self):
        for changes in (dict(stock_bid=replace(self.stock, asof_monotonic_ns=self.now - 2_000_000_001)),
                        dict(usd_cny=replace(self.fx, asof_monotonic_ns=self.now - 5_000_000_001)),
                        dict(usd_cny=replace(self.fx, asof_monotonic_ns=self.now + 1)),
                        dict(stock_bid=replace(self.stock, base="999"))):
            result = self.check(**changes)
            self.assertEqual(result.phase, Phase.WAIT)
            self.assertFalse(result.state.triggered)

    def test_invalid_prices_and_rates_cannot_trigger(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1"), 5.0):
            result = self.check(usd_cny=replace(self.fx, value=value))
            self.assertEqual(result.phase, Phase.WAIT)

    def test_latch_survives_price_fall_and_missing_fx(self):
        triggered = self.check().state
        result = self.check(state=triggered, stock_bid=replace(self.stock, value=Decimal("80")), usd_cny=None)
        self.assertEqual(result.target_position, 0)
        self.assertTrue(result.block_new_buys)
        self.assertEqual(result.max_additional_sell_quantity, 10000)

    def test_cancel_buys_barrier_includes_pending_cancel_unknown_and_reserved(self):
        for status in ("WORKING", "CANCEL_PENDING", "SEND_UNKNOWN", "RESERVED"):
            order = OpenOrder("buy-1", "BUY", 10, status)
            result = self.check(holding=replace(self.holding, orders=(order,)))
            self.assertEqual(result.cancel_buy_ids, ("buy-1",))
            self.assertEqual(result.phase, Phase.CANCEL_BUYS)
            self.assertEqual(result.max_additional_sell_quantity, 0)

    def test_cancel_race_late_buy_fills_use_new_reconciled_quantity(self):
        first = self.check(holding=replace(self.holding, orders=(OpenOrder("buy-1", "BUY", 10),)))
        result = self.check(state=first.state, holding=replace(self.holding, quantity=10010))
        self.assertEqual(result.max_additional_sell_quantity, 10010)

    def test_partial_sells_and_pending_cancels_keep_reservations(self):
        result = self.check(state=self.check().state, holding=replace(
            self.holding, quantity=7000, orders=(OpenOrder("sell-1", "SELL", 5000, "CANCEL_PENDING"),)))
        self.assertEqual(result.max_additional_sell_quantity, 2000)
        self.assertEqual(result.target_position, 0)

    def test_repeated_target_does_not_release_or_double_count_reservations(self):
        triggered = self.check().state
        holding = replace(self.holding, orders=(OpenOrder("sell-1", "SELL", 10000, "RESERVED"),))
        for _ in range(2):
            result = self.check(state=triggered, holding=holding)
            self.assertEqual(result.max_additional_sell_quantity, 0)

    def test_unknown_sell_result_blocks_additional_sells(self):
        holding = replace(self.holding, orders=(OpenOrder("sell-1", "SELL", 10, "SEND_UNKNOWN"),))
        result = self.check(holding=holding)
        self.assertEqual(result.phase, Phase.WAIT)
        self.assertEqual(result.max_additional_sell_quantity, 0)
        self.assertTrue(result.state.triggered)

    def test_excess_sells_must_be_cancelled_not_silently_clamped(self):
        holding = replace(self.holding, orders=(OpenOrder("sell-1", "SELL", 10001),))
        result = self.check(holding=holding)
        self.assertEqual(result.cancel_sell_ids, ("sell-1",))
        self.assertEqual(result.phase, Phase.WAIT)

    def test_buy_barrier_preserves_excess_and_zero_position_sell_cancellations(self):
        for quantity, leaves in ((10000, 10001), (0, 0)):
            holding = replace(self.holding, quantity=quantity, orders=(
                OpenOrder("buy", "BUY", 10), OpenOrder("sell", "SELL", leaves)))
            result = self.check(state=self.check().state, holding=holding)
            self.assertEqual(result.cancel_buy_ids, ("buy",))
            self.assertEqual(result.cancel_sell_ids, ("sell",))
            self.assertEqual(result.max_additional_sell_quantity, 0)

    def test_unknown_oversized_sell_retains_cancellation_intent(self):
        holding = replace(self.holding, orders=(OpenOrder("sell", "SELL", 10001, "SEND_UNKNOWN"),))
        result = self.check(holding=holding)
        self.assertEqual(result.phase, Phase.WAIT)
        self.assertEqual(result.cancel_sell_ids, ("sell",))
        self.assertEqual(result.max_additional_sell_quantity, 0)

    def test_flat_requires_no_residual_orders_and_remains_locked(self):
        state = self.check().state
        empty = replace(self.holding, quantity=0)
        result = self.check(state=state, holding=empty, stock_bid=None, usd_cny=None)
        self.assertEqual(result.phase, Phase.FLAT_LOCKED)
        self.assertTrue(result.block_new_buys)
        for side in ("BUY", "SELL"):
            result = self.check(state=state, holding=replace(empty, orders=(OpenOrder("residual", side, 0),)))
            self.assertNotEqual(result.phase, Phase.FLAT_LOCKED)

    def test_latched_exit_waits_for_reconciliation_health_and_trading_status(self):
        state = self.check().state
        for kwargs in (dict(reconciled=False), dict(broker_healthy=False), dict(tradable=False),
                       dict(asof_monotonic_ns=self.now - 2_000_000_001)):
            result = self.check(state=state, holding=replace(self.holding, **kwargs))
            self.assertEqual(result.phase, Phase.WAIT)
            self.assertTrue(result.state.triggered)
            self.assertTrue(result.block_new_buys)
            self.assertEqual(result.max_additional_sell_quantity, 0)

    def test_cancel_intent_retained_with_stale_prices_and_disconnected_broker(self):
        result = self.check(state=self.check().state, stock_bid=None, usd_cny=None,
                            holding=replace(self.holding, broker_healthy=False,
                                            orders=(OpenOrder("buy", "BUY", 1),)))
        self.assertEqual(result.cancel_buy_ids, ("buy",))
        self.assertFalse(result.execution_authorized)

    def test_saved_state_roundtrip_keeps_latch_and_identity(self):
        restored = ExitState(**asdict(self.check().state))
        result = self.check(state=restored, usd_cny=None)
        self.assertEqual(result.phase, Phase.EXIT_WORKING)
        with self.assertRaises(ExitFault):
            self.check(state=replace(restored, con_id=999))
        with self.assertRaises(ExitFault):
            self.check(state=replace(restored, account="OTHER"))

    def test_negative_fractional_duplicate_or_bad_snapshots_rejected(self):
        for value in (-1, Decimal("1.5"), True):
            with self.assertRaises(ExitFault):
                self.check(holding=replace(self.holding, quantity=value))
        order = OpenOrder("duplicate", "SELL", 1)
        with self.assertRaises(ExitFault):
            self.check(holding=replace(self.holding, orders=(order, order)))
        with self.assertRaises(ExitFault):
            self.check(holding=replace(self.holding, reconciled="true"))

    def test_risk_configuration_rejects_inexact_threshold_and_invalid_ttl(self):
        for kwargs in (dict(threshold_cny=5000000.0), dict(fx_max_age_ns=0), dict(stock_max_age_ns=True)):
            with self.assertRaises(ExitFault):
                ExitRule(**kwargs)


if __name__ == "__main__":
    unittest.main()
