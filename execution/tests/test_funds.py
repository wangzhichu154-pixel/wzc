import unittest

from execution.funds import AccountResources, FundsError, TradingPermissions, money_units


class FundsTests(unittest.TestCase):
    def setUp(self):
        self.funds = AccountResources("DU123456", max_age_ns=1000)

    def snapshot(self, request_id=1, now=100, values=None):
        self.funds.begin(request_id, now)
        values = values or {"AvailableFunds": "100", "SettledCash": "80", "BuyingPower": "400"}
        for tag, value in values.items():
            self.funds.receive(request_id, "DU123456", tag, value, "USD")
        return self.funds.complete(request_id, now + 10)

    def test_initial_missing_and_partial_snapshot_fail_closed(self):
        self.assertFalse(self.funds.fresh(100))
        self.assertFalse(self.snapshot(values={"AvailableFunds": "100"}))
        self.assertIsNone(self.funds.remaining(111, 0))

    def test_exact_account_and_currency_required(self):
        self.funds.begin(1, 100)
        self.assertFalse(self.funds.receive(1, "DUOTHER", "AvailableFunds", "100000", "USD"))
        with self.assertRaises(FundsError):
            self.funds.receive(1, "DU123456", "AvailableFunds", "100", "BASE")
        for tag in ("AvailableFunds", "SettledCash", "BuyingPower"):
            self.funds.receive(1, "DU123456", tag, "100", "USD")
        self.assertFalse(self.funds.complete(1, 110))

    def test_buying_power_is_not_cash(self):
        self.assertTrue(self.snapshot())
        self.assertEqual(self.funds.remaining(111, 0), 800000)
        self.assertFalse(self.funds.can_reserve_buy(111, 700000, 100001))

    def test_pending_and_spent_and_fee_debits_survive_new_snapshots(self):
        self.snapshot()
        self.funds.record_buy_fill(10, 10000)
        self.funds.observe_positive_fees(100)
        self.assertEqual(self.funds.remaining(120, 50000), 649900)
        self.snapshot(2, 200, {tag: "1000000" for tag in ("AvailableFunds", "SettledCash", "BuyingPower")})
        self.funds.observe_positive_fees(50)
        self.assertEqual(self.funds.remaining(220, 50000), 649900)

    def test_duplicate_conflicting_field_keeps_lower_value(self):
        self.funds.begin(1, 100)
        self.funds.receive(1, "DU123456", "AvailableFunds", "50", "USD")
        self.funds.receive(1, "DU123456", "AvailableFunds", "5000", "USD")
        for tag in ("SettledCash", "BuyingPower"):
            self.funds.receive(1, "DU123456", tag, "100", "USD")
        self.assertTrue(self.funds.complete(1, 110))
        self.assertEqual(self.funds.remaining(120, 0), 500000)

    def test_old_generation_and_post_end_updates_cannot_refresh_ttl(self):
        self.snapshot()
        self.assertFalse(self.funds.receive(1, "DU123456", "AvailableFunds", "9999", "USD"))
        self.assertFalse(self.funds.complete(1, 999))
        self.assertFalse(self.funds.fresh(1101))
        self.snapshot(2, 1200)
        self.assertFalse(self.funds.receive(1, "DU123456", "AvailableFunds", "9999", "USD"))
        with self.assertRaises(FundsError):
            self.funds.begin(1, 1300)

    def test_slow_snapshot_does_not_gain_freshness_from_end_callback(self):
        self.funds.begin(1, 100)
        for tag in ("AvailableFunds", "SettledCash", "BuyingPower"):
            self.funds.receive(1, "DU123456", tag, "100", "USD")
        self.assertFalse(self.funds.complete(1, 1101))

    def test_newer_snapshot_does_not_increase_a_lower_observed_ceiling(self):
        self.snapshot()
        self.snapshot(2, 200, {tag: "40" for tag in ("AvailableFunds", "SettledCash", "BuyingPower")})
        self.snapshot(3, 300, {tag: "100" for tag in ("AvailableFunds", "SettledCash", "BuyingPower")})
        self.assertEqual(self.funds.remaining(320, 0), 400000)

    def test_invalid_money_and_conservative_rounding(self):
        for value in ("NaN", "Infinity", "text", "1e100"):
            with self.subTest(value=value), self.assertRaises(FundsError):
                money_units(value)
        self.assertEqual(money_units("1.00009"), 10000)
        self.assertEqual(money_units("-0.00001"), -1)

    def test_permission_not_inferred_from_funds_or_model(self):
        denied = TradingPermissions("DU123456", False)
        self.assertFalse(denied.may_buy("DU123456"))
        allowed = TradingPermissions("DU123456", True)
        self.assertFalse(allowed.may_buy("OTHER"))
        self.assertFalse(allowed.may_sell("DU123456", position=0, reserved_sells=0, quantity=1))
        self.assertFalse(allowed.may_sell("DU123456", position=5, reserved_sells=4, quantity=2))
        self.assertTrue(allowed.may_sell("DU123456", position=5, reserved_sells=4, quantity=1))


if __name__ == "__main__":
    unittest.main()
