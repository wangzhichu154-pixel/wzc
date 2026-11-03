"""Actual C++ parser -> causal observations -> side gate -> simulator tests.

All input generated here is protocol test data, not market/profitability evidence.
"""
import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from make_fixture import header
from make_simulation_fixture import frames

BINARY = os.environ.get('TRADING_ML_REPLAY_BIN', str(Path(__file__).resolve().parents[2] / 'build' / 'trading-ml-replay'))
ORDINARY = os.environ.get('TRADING_REPLAY_BIN', str(Path(__file__).resolve().parents[2] / 'build' / 'trading-replay'))
SIGNAL_HEADER = 'ordinal,allow_bid,allow_ask,expires_ns\n'
MARKET_HEADER = 'ordinal,timestamp_ns,logical_timestamp_ns,stock_locate,bid,ask,bid_qty,ask_qty,valid,tradable'


class MLReplayCLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.capture = self.directory / 'capture.itch'
        self.signals = self.directory / 'signals.csv'
        self.export = self.directory / 'market.csv'
        self.write_capture(list(frames()))

    def write_capture(self, messages):
        self.capture.write_bytes(b''.join(struct.pack('>H', len(m)) + m for m in messages))

    def run_cli(self, *args):
        return subprocess.run([BINARY, '--input', str(self.capture), '--symbol-locate', '1', *args],
                              capture_output=True, text=True, timeout=15)

    def simulation(self, rows='5,1,1,1000000000\n', *args):
        self.signals.write_text(SIGNAL_HEADER + rows)
        result = self.run_cli('--simulate', '--signal-file', str(self.signals), *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_causal_export_includes_invalid_and_same_timestamp_rows(self):
        messages = list(frames())
        messages.insert(5, header('A', timestamp=1_000_000) + struct.pack('>QcI8sI', 99, b'B', 1, b'TEST    ', 1_000_000))
        self.write_capture(messages)
        result = self.run_cli('--export-market', str(self.export))
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = self.export.read_text()
        self.assertEqual(raw.splitlines()[0], MARKET_HEADER)
        rows = list(csv.DictReader(io.StringIO(raw)))
        self.assertEqual([int(r['ordinal']) for r in rows[:5]], [1, 2, 3, 5, 6])
        self.assertEqual(rows[2]['valid'], '0')
        self.assertEqual(rows[4]['valid'], '1')
        self.assertEqual(rows[4]['tradable'], '1')
        self.assertEqual(rows[3]['timestamp_ns'], rows[4]['timestamp_ns'])
        self.assertTrue(all(r['stock_locate'] == '1' for r in rows))
        self.assertNotIn(7, [int(r['ordinal']) for r in rows])  # P print is not L1 volume.
        metadata = json.loads(result.stdout)['summary']['ml']
        self.assertEqual(metadata['export_rows'], len(rows))
        self.assertFalse(metadata['capture_binding_verified_by_engine'])

    def test_halt_resume_and_regressing_raw_time_export_reset(self):
        messages = list(frames())
        messages += [header('H', timestamp=11_000_000) + b'TEST    H     ',
                     header('H', timestamp=10_500_000) + b'TEST    T     ']
        self.write_capture(messages)
        result = self.run_cli('--export-market', str(self.export))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = list(csv.DictReader(self.export.read_text().splitlines()))
        self.assertEqual(rows[-2]['tradable'], '0')
        self.assertEqual(rows[-1]['tradable'], '1')
        self.assertEqual(rows[-1]['timestamp_ns'], '10500000')
        self.assertEqual(rows[-1]['logical_timestamp_ns'], '11000000')

    def test_invalid_capture_never_publishes_export(self):
        self.capture.write_bytes(self.capture.read_bytes()[:-1])
        result = self.run_cli('--export-market', str(self.export))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertFalse(self.export.exists())
        self.assertEqual(list(self.directory.glob('*.partial.*')), [])

    def test_export_refuses_existing_destination(self):
        self.export.write_text('retain original')
        result = self.run_cli('--export-market', str(self.export))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.export.read_text(), 'retain original')

    def test_wrong_locate_is_rejected(self):
        result = self.run_cli('--symbol-locate', '2', '--export-market', str(self.export))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.export.exists())

    def test_empty_signal_file_denies_all_entries(self):
        data = self.simulation('')
        stats = data['summary']['simulation']
        self.assertEqual(stats['orders_submitted'], 0)
        self.assertEqual(stats['fill_count'], 0)
        self.assertEqual(stats['fees_units'], 0)
        self.assertEqual(stats['net_pnl_units'], 0)
        self.assertTrue(stats['signal_gate']['enabled'])

    def test_gate_does_not_replace_fifo_or_accounting_and_is_deterministic(self):
        first, second = self.simulation(), self.simulation()
        self.assertEqual(first['checksum'], second['checksum'])
        stats = first['summary']['simulation']
        self.assertEqual(stats['fill_count'], 2)
        self.assertEqual(stats['fees_units'], 1800)
        self.assertEqual(stats['net_pnl_units'], 4200)
        self.assertEqual(first['summary']['orders_sent'], 0)
        metrics = stats['execution_metrics']
        self.assertEqual(metrics['buy_quantity'], 30)
        self.assertEqual(metrics['sell_quantity'], 30)
        self.assertEqual(metrics['turnover_units'], 60_006_000)
        self.assertEqual(metrics['closed_inventory_episodes'], 1)
        self.assertEqual(metrics['holding_total_ns'], 4_000_000)
        self.assertEqual(metrics['peak_abs_inventory_shares'], 30)
        self.assertAlmostEqual(metrics['inventory_share_seconds'], .12)
        self.assertEqual(metrics['time_weighted_abs_inventory_shares'], 12)

    def test_future_row_is_not_applied_early(self):
        stats = self.simulation('10,1,1,1000000000\n')['summary']['simulation']
        self.assertEqual(stats['execution_metrics']['buy_quantity'], 0)
        self.assertEqual(stats['fill_count'], 0)

    def test_one_side_gate_cannot_generate_opposite_fill(self):
        stats = self.simulation('5,1,0,1000000000\n')['summary']['simulation']
        self.assertEqual(stats['execution_metrics']['sell_quantity'], 0)
        self.assertEqual(stats['inventory'], 30)
        self.assertEqual(stats['execution_metrics']['open_holding_ns'], 4_000_000)

    def test_expired_signal_denies_entry(self):
        stats = self.simulation('5,1,1,1000000\n')['summary']['simulation']
        self.assertEqual(stats['orders_submitted'], 0)
        self.assertEqual(stats['signal_gate']['expired_signal_rows'], 1)

    def test_expiry_cancels_existing_orders_before_later_executions(self):
        stats = self.simulation('5,1,1,4500000\n', '--cancel-latency-ns', '1')['summary']['simulation']
        self.assertEqual(stats['fill_count'], 0)
        self.assertGreater(stats['cancel_requests'], 0)
        self.assertEqual(stats['open_orders_at_end'], 0)

    def test_explicit_deny_cancels_old_quotes(self):
        stats = self.simulation('5,1,1,1000000000\n7,0,0,1000000000\n')['summary']['simulation']
        self.assertEqual(stats['fill_count'], 0)
        self.assertGreater(stats['orders_canceled'], 0)

    def test_halt_clears_signal_until_an_explicit_new_prediction(self):
        messages = list(frames())
        messages[5:5] = [header('H', timestamp=2_100_000) + b'TEST    H     ',
                         header('H', timestamp=2_200_000) + b'TEST    T     ']
        self.write_capture(messages)
        stats = self.simulation('5,1,1,1000000000\n', '--cancel-latency-ns', '1')['summary']['simulation']
        self.assertEqual(stats['fill_count'], 0)
        self.assertEqual(stats['orders_submitted'], 2)

    def test_evaluation_window_blocks_early_entry(self):
        stats = self.simulation('5,1,1,1000000000\n', '--trade-start-ns', '11000000')['summary']['simulation']
        self.assertEqual(stats['orders_submitted'], 0)
        self.assertEqual(stats['execution_metrics']['observed_window_duration_ns'], 0)
        self.assertIsNone(stats['execution_metrics']['time_weighted_abs_inventory_shares'])

    def test_window_end_cancels_but_keeps_tail_fill_exposure(self):
        data = self.simulation('5,1,1,1000000000\n', '--trade-end-ns', '7000000', '--cancel-latency-ns', '100000000')
        stats = data['summary']['simulation']
        self.assertEqual(data['messages'], 12)  # Never cut replay off at window end.
        self.assertEqual(stats['orders_submitted'], 2)
        self.assertEqual(stats['signal_gate']['inventory_at_window_end'], 30)
        self.assertEqual(stats['signal_gate']['fills_after_window_end'], 1)
        self.assertEqual(stats['signal_gate']['quantity_after_window_end'], 30)
        self.assertEqual(stats['signal_gate']['turnover_after_window_end_units'], 30_006_000)
        self.assertEqual(stats['execution_metrics']['observed_window_duration_ns'], 7_000_000)
        self.assertAlmostEqual(stats['execution_metrics']['inventory_share_seconds'], .03)
        self.assertGreater(stats['fills_while_cancel_pending'], 0)

    def test_long_only_does_not_fund_sells_with_unfilled_buys(self):
        stats = self.simulation('5,1,1,1000000000\n', '--long-only')['summary']['simulation']
        self.assertTrue(stats['config']['long_only'])
        self.assertEqual(stats['execution_metrics']['sell_quantity'], 0)
        self.assertEqual(stats['inventory'], 30)  # Size100 is rejected against actual30, not resized.
        self.assertGreater(stats['long_only_rejections'], 0)

    def test_long_only_can_sell_confirmed_inventory(self):
        stats = self.simulation('5,1,1,1000000000\n', '--long-only', '--max-order-size', '30')['summary']['simulation']
        self.assertEqual(stats['execution_metrics']['buy_quantity'], 30)
        self.assertEqual(stats['execution_metrics']['sell_quantity'], 30)
        self.assertEqual(stats['inventory'], 0)
        self.assertGreaterEqual(stats['worst_short_inventory'], 0)

    def test_closed_zero_trade_account_has_reconciled_cash_valuation(self):
        messages = list(frames()) + [header('S', 0, timestamp=11_000_000) + b'M',
                                     header('S', 0, timestamp=13_000_000) + b'C']
        self.write_capture(messages)
        stats = self.simulation('')['summary']['simulation']
        self.assertFalse(stats['pnl_mark_valid'])  # Trading/legacy semantics unchanged.
        self.assertEqual(stats['orders_submitted'], 0)
        self.assertEqual(stats['valuation'], {
            'schema_version': 1, 'basis': 'reconciled_flat_ledger',
            'quote_mark_valid': False, 'net_pnl_valid': True,
            'unrealized_pnl_units': 0, 'net_pnl_units': 0,
            'drawdown_complete': True, 'max_drawdown_units': 0,
        })

    def test_closed_completed_roundtrip_values_actual_ledger_not_quote(self):
        self.write_capture(list(frames()) + [header('S', 0, timestamp=11_000_000) + b'M',
                                             header('S', 0, timestamp=13_000_000) + b'C'])
        stats = self.simulation('5,1,1,1000000000\n12,0,0,1000000000\n')['summary']['simulation']
        valuation = stats['valuation']
        self.assertEqual(stats['fill_count'], 2)
        self.assertEqual(stats['inventory'], 0)
        self.assertEqual(stats['open_orders_at_end'], 0)
        self.assertEqual(stats['cash_before_fees_units'], stats['realized_pnl_units'])
        self.assertFalse(stats['pnl_mark_valid'])
        self.assertEqual(valuation['basis'], 'reconciled_flat_ledger')
        self.assertTrue(valuation['net_pnl_valid'])
        self.assertEqual(valuation['unrealized_pnl_units'], 0)
        self.assertEqual(valuation['net_pnl_units'], 4200)
        self.assertEqual(valuation['net_pnl_units'], stats['realized_pnl_units'] - stats['fees_units'] + stats['rebates_units'])
        self.assertTrue(valuation['drawdown_complete'])
        self.assertEqual(valuation['max_drawdown_units'], stats['drawdown_units'])

    def test_closed_nonflat_account_cannot_claim_cash_only_valuation(self):
        self.write_capture(list(frames()) + [header('S', 0, timestamp=11_000_000) + b'M',
                                             header('S', 0, timestamp=13_000_000) + b'C'])
        stats = self.simulation('5,1,0,1000000000\n12,0,0,1000000000\n')['summary']['simulation']
        self.assertEqual(stats['inventory'], 30)
        self.assertEqual(stats['open_orders_at_end'], 0)
        self.assertEqual(stats['valuation']['basis'], 'unavailable')
        for name in ('net_pnl_units', 'unrealized_pnl_units', 'max_drawdown_units'):
            self.assertIsNone(stats['valuation'][name])
        self.assertFalse(stats['valuation']['net_pnl_valid'])
        self.assertFalse(stats['valuation']['drawdown_complete'])

    def test_pending_cancel_prevents_terminal_flat_ledger_valuation(self):
        self.write_capture(list(frames()) + [header('S', 0, timestamp=11_000_000) + b'M'])
        stats = self.simulation('5,1,1,1000000000\n12,0,0,1000000000\n')['summary']['simulation']
        self.assertEqual(stats['inventory'], 0)
        self.assertGreater(stats['open_orders_at_end'], 0)
        self.assertGreater(stats['cancel_requests'], 0)
        self.assertEqual(stats['valuation']['basis'], 'unavailable')
        self.assertFalse(stats['valuation']['net_pnl_valid'])
        self.assertFalse(stats['valuation']['drawdown_complete'])

    def test_current_fresh_valuation_keeps_existing_ledger_and_open_risk(self):
        stats = self.simulation()['summary']['simulation']
        self.assertGreater(stats['open_orders_at_end'], 0)
        valuation = stats['valuation']
        self.assertEqual(valuation['basis'], 'fresh_bbo')
        self.assertTrue(valuation['quote_mark_valid'])
        self.assertTrue(valuation['net_pnl_valid'])
        self.assertEqual(valuation['net_pnl_units'], stats['net_pnl_units'])
        self.assertEqual(valuation['unrealized_pnl_units'], stats['unrealized_pnl_units'])
        self.assertTrue(valuation['drawdown_complete'])
        self.assertEqual(valuation['max_drawdown_units'], stats['drawdown_units'])

    def test_halt_while_holding_permanently_invalidates_drawdown_path(self):
        messages = list(frames())
        # The modeled buy is filled at row9 / 6ms. Halt/resume while holding,
        # then original later sell executions flatten the account after resume.
        messages[9:9] = [header('H', timestamp=6_500_000) + b'TEST    H     ',
                          header('H', timestamp=7_000_000) + b'TEST    T     ']
        messages += [header('S', 0, timestamp=111_000_000) + b'M',
                     header('S', 0, timestamp=113_000_000) + b'C']
        self.write_capture(messages)
        # The extended cancel delay retains the resting sell through the halt;
        # any resulting fill is still supplied by the actual FIFO engine.
        stats = self.simulation('5,1,1,1000000000\n14,0,0,1000000000\n',
                                '--cancel-latency-ns', '100000000')['summary']['simulation']
        self.assertEqual(stats['fill_count'], 2)
        self.assertEqual(stats['inventory'], 0)
        self.assertEqual(stats['open_orders_at_end'], 0)
        self.assertEqual(stats['valuation']['basis'], 'reconciled_flat_ledger')
        self.assertEqual(stats['valuation']['net_pnl_units'], 4200)
        self.assertFalse(stats['valuation']['drawdown_complete'])
        self.assertIsNone(stats['valuation']['max_drawdown_units'])

    def test_timestamp_gap_cannot_be_erased_by_a_new_fresh_quote(self):
        messages = list(frames())
        # Preserve order identities but shift post-buy events by two seconds.
        # New L1 changes at the first later row would otherwise hide staleness.
        for index in range(9, len(messages)):
            raw = bytearray(messages[index])
            original = int.from_bytes(raw[5:11], 'big')
            raw[5:11] = (original + 2_000_000_000).to_bytes(6, 'big')
            messages[index] = bytes(raw)
        self.write_capture(messages)
        stats = self.simulation('5,1,0,1000000000\n')['summary']['simulation']
        self.assertEqual(stats['inventory'], 30)
        self.assertTrue(stats['pnl_mark_valid'])
        self.assertEqual(stats['valuation']['basis'], 'fresh_bbo')
        self.assertTrue(stats['valuation']['net_pnl_valid'])
        self.assertFalse(stats['valuation']['drawdown_complete'])
        self.assertIsNone(stats['valuation']['max_drawdown_units'])

    def test_signal_cannot_override_daily_loss_or_costs(self):
        stats = self.simulation('5,1,1,1000000000\n', '--daily-loss-limit-cents', '5')['summary']['simulation']
        self.assertTrue(stats['killed'])
        self.assertEqual(stats['fill_count'], 1)
        stats = self.simulation('5,1,1,1000000000\n', '--min-fee-per-order-units', '10000')['summary']['simulation']
        self.assertEqual(stats['fill_count'], 0)
        self.assertTrue(stats['pricing_economics']['zero_fill_outcome'])

    def test_invalid_signal_schema_order_ranges_fail_without_result(self):
        for content in [
            'sequence,bid,ask,expiry\n',
            SIGNAL_HEADER + '5,1,1,1000000000\n5,1,1,1000000000\n',
            SIGNAL_HEADER + '5,1,1,1000000000\n4,1,1,1000000000\n',
            SIGNAL_HEADER + '5,NaN,1,1000000000\n',
            SIGNAL_HEADER + '5,1,1,-1\n',
            SIGNAL_HEADER + '5,1,1,1001000001\n',  # Exceeds default horizon.
            SIGNAL_HEADER + '13,1,1,1000000000\n',
            SIGNAL_HEADER + '5,1,1,1000000000,extra\n',
            SIGNAL_HEADER + '9' * 257 + '\n',
        ]:
            with self.subTest(content=content[:80]):
                self.signals.write_text(content)
                result = self.run_cli('--simulate', '--signal-file', str(self.signals))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')

    def test_ml_arguments_are_not_ordinary_portal_replay_arguments(self):
        result = subprocess.run([ORDINARY, '--input', str(self.capture), '--symbol-locate', '1'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        result = self.run_cli('--simulate', '--export-market', str(self.export))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('explicit --signal-file', result.stderr)


if __name__ == '__main__':
    unittest.main()
