"""CLI integration tests against the actual compiled C++ executable."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from make_fixture import write
from make_simulation_fixture import write as write_simulation

BINARY = os.environ.get('TRADING_REPLAY_BIN', str(Path(__file__).resolve().parents[2] / 'build' / 'trading-replay'))

class ReplayCLI(unittest.TestCase):
    def run_cli(self, path, *args):
        return subprocess.run([BINARY, '--input', str(path), *args], capture_output=True, text=True, timeout=15)

    def test_real_decode_and_determinism(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.itch'
            write(path)
            a, b = self.run_cli(path), self.run_cli(path)
            self.assertEqual(a.returncode, 0, a.stderr)
            x, y = json.loads(a.stdout), json.loads(b.stdout)
            self.assertEqual(x['messages'], 8)
            self.assertEqual(x['trades'], 1)
            self.assertTrue(x['book_valid'])
            self.assertEqual(x['checksum'], y['checksum'])
            self.assertEqual(x['summary']['orders_sent'], 0)
            self.assertIsNone(x['summary']['pnl'])
            self.assertEqual(x['summary']['reported_trade_volume'], 30)

    def test_truncated_capture_fails_without_success_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.itch'
            write(path)
            path.write_bytes(path.read_bytes()[:-1])
            result = self.run_cli(path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('truncated', result.stderr)

    def test_limit_and_parameter_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.itch'
            write(path)
            self.assertNotEqual(self.run_cli(path, '--max-messages', '2').returncode, 0)
            self.assertNotEqual(self.run_cli(path, '--soft-inventory-limit', '2000').returncode, 0)
            result = self.run_cli(path, '--orders-per-second', '1', '--max-order-size', '50')
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)['summary']
            self.assertEqual(summary['effective_risk_parameters']['max_order_size'], 50)
            self.assertGreater(summary['rate_rejected_intents'], 0)

    def test_missing_session_not_claimed_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.itch'
            write(path)
            path.write_bytes(path.read_bytes()[14:])
            result = self.run_cli(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['book_valid'])
            self.assertEqual(json.loads(result.stdout)['decisions'], 0)

    def test_simulation_no_forced_fills_or_profit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.itch'
            write(path)
            result = self.run_cli(path, '--simulate')
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)['summary']
            self.assertEqual(summary['mode'], 'simulation')
            self.assertEqual(summary['simulation']['fill_count'], 0)
            self.assertEqual(summary['simulation']['net_pnl_units'], 0)
            self.assertEqual(summary['orders_sent'], 0)

    def test_simulation_fifo_fees_and_determinism(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.itch'
            write_simulation(path)
            a = self.run_cli(path, '--simulate')
            b = self.run_cli(path, '--simulate')
            self.assertEqual(a.returncode, 0, a.stderr)
            first, second = json.loads(a.stdout), json.loads(b.stdout)
            self.assertEqual(first['checksum'], second['checksum'])
            stats = first['summary']['simulation']
            self.assertEqual(stats['fill_count'], 2)
            self.assertEqual(stats['filled_quantity'], 60)
            self.assertEqual(stats['inventory'], 0)
            self.assertEqual(stats['realized_pnl_units'], 6000)
            self.assertEqual(stats['fees_units'], 1800)
            self.assertEqual(stats['rebates_units'], 0)
            self.assertEqual(stats['net_pnl_units'], 4200)
            self.assertTrue(stats['pnl_mark_valid'])

    def test_simulation_loss_latches_kill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.itch'
            write_simulation(path)
            result = self.run_cli(path, '--simulate', '--daily-loss-limit-cents', '5')
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads(result.stdout)['summary']['simulation']
            self.assertTrue(stats['killed'])
            self.assertEqual(stats['fill_count'], 1)
            self.assertEqual(stats['inventory'], 30)
            self.assertLessEqual(stats['net_pnl_units'], -500)
            self.assertEqual(stats['open_orders_at_end'], 0)

    def test_simulation_partial_fill_keeps_cancel_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'partial.itch'
            from make_simulation_fixture import frames
            import struct
            path.write_bytes(b''.join(struct.pack('>H', len(frame)) + frame for frame in list(frames())[:9]))
            result = self.run_cli(path, '--simulate')
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads(result.stdout)['summary']['simulation']
            self.assertEqual(stats['inventory'], 30)
            self.assertEqual(stats['worst_long_inventory'], 100)
            self.assertGreater(stats['open_orders_at_end'], 0)

    def test_simulation_pending_cancel_can_still_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'race.itch'
            write_simulation(path)
            result = self.run_cli(path, '--simulate', '--cancel-latency-ns', '100000000')
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads(result.stdout)['summary']['simulation']
            self.assertGreater(stats['fills_while_cancel_pending'], 0)
            self.assertEqual(stats['fill_count'], 2)

    def test_simulation_high_fees_widen_quotes_without_forcing_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fees.itch'
            write_simulation(path)
            result = self.run_cli(path, '--simulate', '--fee-per-share-units', '1000')
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads(result.stdout)['summary']['simulation']
            self.assertEqual(stats['fill_count'], 0)
            self.assertEqual(stats['fees_units'], 0)
            self.assertEqual(stats['net_pnl_units'], 0)
            self.assertEqual(stats['pricing_economics']['cost_half_spread_floor_units'], 1000)
            self.assertGreater(stats['pricing_economics']['cost_widened_decisions'], 0)
            self.assertTrue(stats['pricing_economics']['zero_fill_outcome'])

    def test_simulation_minimum_commission_charged_once_per_order(self):
        from make_simulation_fixture import frames
        from make_fixture import header
        import struct
        original = list(frames())[:9]
        widened = []
        for raw in original:
            frame = bytearray(raw)
            if frame[0] == ord('A') and frame[19] == ord('S'):
                frame[32:36] = struct.pack('>I', 1_002_000)
            if frame[0] == ord('P'):
                frame[32:36] = struct.pack('>I', 1_001_000)
            widened.append(bytes(frame))
        first_fill = bytearray(widened[-1])
        first_fill[19:23] = struct.pack('>I', 3)
        widened[-1] = bytes(first_fill)
        widened.append(header('E', timestamp=7_000_000) + struct.pack('>QIQ', 3, 2, 2010))
        widened.append(header('E', timestamp=8_000_000) + struct.pack('>QIQ', 3, 10, 2011))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'partials.itch'
            for count, shares, fees in ((9, 3, 100), (10, 5, 100), (11, 15, 150)):
                path.write_bytes(b''.join(struct.pack('>H', len(frame)) + frame for frame in widened[:count]))
                result = self.run_cli(path, '--simulate', '--fee-per-share-units', '10',
                                      '--min-fee-per-order-units', '100', '--cancel-latency-ns', '100000000')
                self.assertEqual(result.returncode, 0, result.stderr)
                stats = json.loads(result.stdout)['summary']['simulation']
                self.assertEqual(stats['filled_quantity'], shares)
                self.assertEqual(stats['fees_units'], fees)
                self.assertEqual(stats['net_pnl_units'], -fees)
                self.assertEqual(stats['pricing_economics']['fee_charged_orders'], 1)

    def test_simulation_zero_fills_do_not_charge_order_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'no-fills.itch'
            write(path)
            result = self.run_cli(path, '--simulate', '--min-fee-per-order-units', '5000')
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads(result.stdout)['summary']['simulation']
            self.assertEqual(stats['fill_count'], 0)
            self.assertEqual(stats['fees_units'], 0)
            self.assertEqual(stats['pricing_economics']['fee_charged_orders'], 0)

    def test_quote_economic_flags_reject_invalid_or_bound_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bounds.itch'
            write(path)
            for flag in ('--min-fee-per-order-units', '--slippage-buffer-units', '--min-net-edge-units'):
                for value in ('-1', '1000000001', '9223372036854775807'):
                    result = self.run_cli(path, '--simulate', flag, value)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, '')
            result = self.run_cli(path, '--simulate', '--min-fee-per-order-units', '1000000000')
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads(result.stdout)['summary']['simulation']
            self.assertEqual(stats['orders_submitted'], 0)
            self.assertGreater(stats['pricing_economics']['no_valid_quote_decisions'], 0)

    def test_rebates_do_not_narrow_quote_cost_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rebates.itch'
            write_simulation(path)
            a = self.run_cli(path, '--simulate')
            b = self.run_cli(path, '--simulate', '--rebate-per-share-units', '10')
            self.assertEqual(a.returncode, 0, a.stderr)
            self.assertEqual(b.returncode, 0, b.stderr)
            first, second = json.loads(a.stdout), json.loads(b.stdout)
            x, y = first['summary']['simulation'], second['summary']['simulation']
            self.assertEqual(first['checksum'], second['checksum'])
            self.assertEqual(x['fill_count'], y['fill_count'])
            self.assertEqual(y['rebates_units'], 600)
            self.assertEqual(y['net_pnl_units'], x['net_pnl_units'] + 600)
            self.assertFalse(y['pricing_economics']['rebates_reduce_quote_floor'])
            self.assertEqual(y['model_version'], 'displayed_fifo_conservative_v2_cost_aware')
            self.assertEqual(y['config']['cost_policy_version'], 'conservative_one_share_v2')

    def test_simulation_latency_prevents_impossible_early_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'latency.itch'
            write_simulation(path)
            result = self.run_cli(path, '--simulate', '--latency-ns', '1000000000')
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads(result.stdout)['summary']['simulation']
            self.assertEqual(stats['fill_count'], 0)
            self.assertEqual(stats['net_pnl_units'], 0)

    def test_simulation_quote_expiry_is_timer_driven(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'expiry.itch'
            write_simulation(path)
            result = self.run_cli(path, '--simulate', '--simulation-window-ns', '10')
            self.assertEqual(result.returncode, 0, result.stderr)
            stats = json.loads(result.stdout)['summary']['simulation']
            self.assertEqual(stats['fill_count'], 0)
            self.assertGreater(stats['orders_canceled'], 0)

    def test_simulation_bust_of_fill_evidence_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bust.itch'
            write_simulation(path)
            from make_fixture import header
            import struct
            message = header('B', timestamp=11_000_000) + struct.pack('>Q', 2002)
            with path.open('ab') as target:
                target.write(struct.pack('>H', len(message)) + message)
            result = self.run_cli(path, '--simulate')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('execution correction model required', result.stderr)

    def test_simulation_metadata_does_not_refresh_stale_mark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stale.itch'
            write_simulation(path)
            from make_fixture import header
            import struct
            message = header('Y', timestamp=3_000_000_000) + b'TEST    0'
            with path.open('ab') as target:
                target.write(struct.pack('>H', len(message)) + message)
            result = self.run_cli(path, '--simulate')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['summary']['simulation']['pnl_mark_valid'])

    def test_simulation_halt_invalidates_mark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'halt.itch'
            write_simulation(path)
            from make_fixture import header
            import struct
            message = header('H', timestamp=11_000_000) + b'TEST    H     '
            with path.open('ab') as target:
                target.write(struct.pack('>H', len(message)) + message)
            result = self.run_cli(path, '--simulate')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['summary']['simulation']['pnl_mark_valid'])

    def test_simulation_rejects_multi_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.itch'
            write(path)
            from make_fixture import header
            import struct
            message = header('H', locate=2) + b'OTHER   T     '
            with path.open('ab') as target:
                target.write(struct.pack('>H', len(message)) + message)
            result = self.run_cli(path, '--simulate')
            self.assertEqual(result.returncode, 2)
            self.assertIn('single stock locate', result.stderr)

if __name__ == '__main__':
    unittest.main()
