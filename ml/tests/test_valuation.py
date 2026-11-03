"""Accounting contracts only; test ledgers are not market-performance evidence."""
from copy import deepcopy
import unittest

from ml.valuation import aggregate_sessions, validate_valuation


def flat_ledger():
    return {"fill_count": 0, "filled_quantity": 0, "realized_pnl_units": 0,
            "unrealized_pnl_units": 0, "net_pnl_units": 0, "cash_before_fees_units": 0,
            "inventory": 0, "fees_units": 0, "rebates_units": 0, "drawdown_units": 0,
            "open_orders_at_end": 0, "pnl_mark_valid": False, "killed": False,
            "execution_metrics": {"buy_quantity": 0, "sell_quantity": 0,
                "buy_notional_units": 0, "sell_notional_units": 0, "turnover_units": 0,
                "closed_inventory_episodes": 0, "holding_total_ns": 0,
                "peak_abs_inventory_shares": 0, "inventory_share_seconds": 0.0},
            "valuation": {"schema_version": 1, "basis": "reconciled_flat_ledger",
                "quote_mark_valid": False, "net_pnl_valid": True, "unrealized_pnl_units": 0,
                "net_pnl_units": 0, "drawdown_complete": True, "max_drawdown_units": 0}}


class AccountValuationTests(unittest.TestCase):
    def test_market_close_does_not_erase_reconciled_flat_account_value(self):
        value = validate_valuation(flat_ledger())
        self.assertTrue(value["net_pnl_valid"])
        self.assertFalse(value["quote_mark_valid"])
        self.assertEqual(value["net_pnl_units"], 0)

    def test_flat_ledger_cannot_hide_pending_orders_or_fabricate_pnl(self):
        for key, value in (("open_orders_at_end", 1), ("inventory", 1), ("cash_before_fees_units", 100),
                           ("realized_pnl_units", 100), ("fees_units", 100)):
            with self.subTest(key=key):
                ledger = flat_ledger()
                ledger[key] = value
                with self.assertRaises(ValueError):
                    validate_valuation(ledger)

    def test_flat_at_end_cannot_repair_incomplete_drawdown_observations(self):
        ledger = flat_ledger()
        ledger["valuation"].update(drawdown_complete=False, max_drawdown_units=None)
        result = aggregate_sessions([{"capture_id": "closed-after-gap", "events": 100, "simulation": ledger}])
        self.assertEqual(result["net_pnl_units"], 0)
        self.assertFalse(result["valuation"]["drawdown_complete"])
        self.assertIsNone(result["max_drawdown_units"])
        ledger["valuation"]["max_drawdown_units"] = 0
        with self.assertRaises(ValueError):
            validate_valuation(ledger)

    def test_quantities_turnover_and_finite_exposure_must_reconcile(self):
        for key, value in (("buy_quantity", 1), ("buy_notional_units", 1), ("turnover_units", 1),
                           ("inventory_share_seconds", float("nan")), ("inventory_share_seconds", -1)):
            with self.subTest(key=key, value=value):
                ledger = flat_ledger()
                ledger["execution_metrics"][key] = value
                with self.assertRaises(ValueError):
                    validate_valuation(ledger)

    def test_booleans_are_not_accepted_as_monetary_values(self):
        ledger = flat_ledger()
        ledger["fees_units"] = False
        with self.assertRaises(ValueError):
            validate_valuation(ledger)

    def test_mixed_quote_freshness_retains_valid_account_totals(self):
        closed, fresh = flat_ledger(), flat_ledger()
        fresh["pnl_mark_valid"] = True
        fresh["valuation"].update(basis="fresh_bbo", quote_mark_valid=True)
        result = aggregate_sessions([{"capture_id": "closed", "events": 100, "simulation": closed},
                                     {"capture_id": "fresh", "events": 100, "simulation": fresh}])
        self.assertFalse(result["pnl_mark_valid"])
        self.assertTrue(result["valuation"]["net_pnl_valid"])
        self.assertEqual(result["valuation"]["basis"], "mixed_valid")
        self.assertEqual(result["net_pnl_units"], 0)

    def test_duplicate_session_cannot_double_count_results(self):
        row = {"capture_id": "session", "events": 100, "simulation": flat_ledger()}
        with self.assertRaises(ValueError):
            aggregate_sessions([row, deepcopy(row)])


if __name__ == "__main__":
    unittest.main()
