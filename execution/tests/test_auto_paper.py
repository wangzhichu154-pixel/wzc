"""SDK test doubles live only here; production imports official ibapi."""
import ctypes
import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import deque
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from execution.auto_paper import Broker, Bucket, PaperRunner, SafetyFault, Settings
from execution.core_binding import Core, CoreError, Config, Health, Permit, Quote, Snapshot
from execution.live_feed import FeedFault, SessionSequencer
from execution.paper_ledger import Journal, LedgerFault, PnLLedger

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "build/libvector_core.so"


def now_exchange():
    return datetime.now(ZoneInfo("America/New_York"))


def header(kind, size, locate=1, current=None):
    current = current or now_exchange()
    ns = ((current.hour * 60 + current.minute) * 60 + current.second) * 1_000_000_000 + current.microsecond * 1000
    data = bytearray(size)
    data[0:1] = kind.encode()
    data[1:3] = locate.to_bytes(2, "big")
    data[5:11] = ns.to_bytes(6, "big")
    return data


def system(code, current=None):
    raw = header("S", 12, 0, current)
    raw[11] = ord(code)
    return bytes(raw)


def status():
    raw = header("H", 25)
    raw[11:19] = b"TEST    "
    raw[19] = ord("T")
    return bytes(raw)


def add(reference, side, price):
    raw = header("A", 36)
    raw[11:19] = reference.to_bytes(8, "big")
    raw[19] = ord(side)
    raw[20:24] = (100).to_bytes(4, "big")
    raw[24:32] = b"TEST    "
    raw[32:36] = price.to_bytes(4, "big")
    return bytes(raw)


def packet(first, *messages):
    return b"TESTSESS01" + first.to_bytes(8, "big") + len(messages).to_bytes(2, "big") + b"".join(
        len(message).to_bytes(2, "big") + message for message in messages
    )


def settings(directory):
    return Settings(account="DU123456", symbol="TEST", con_id=123, locate=1,
                    group="239.1.1.1", port=18000, interface="127.0.0.1", source_ip="127.0.0.1",
                    session="TESTSESS01", session_date=now_exchange().date().isoformat(),
                    library=LIBRARY, state_dir=directory, order_size=2, inventory_limit=10,
                    price_low=10_000, price_high=1_000_000, fee_per_share_units=0,
                    minimum_fee_units=1, slippage_buffer_units=0, minimum_net_edge_units=0,
                    stock_permission_attested=True)


class FeedTests(unittest.TestCase):
    def make(self):
        current = now_exchange()
        return SessionSequencer(b"TESTSESS01", current.date().isoformat(), 1_000_000_000), current

    def test_complete_start_and_duplicate(self):
        sequencer, current = self.make()
        raw = system("O", current)
        self.assertEqual(sequencer.consume(packet(1, raw), current), [raw])
        self.assertEqual(sequencer.consume(packet(1, raw), current), [])
        self.assertEqual(sequencer.expected, 2)

    def test_gap_latches_and_cannot_resume(self):
        sequencer, current = self.make()
        sequencer.consume(packet(1, system("O", current)), current)
        with self.assertRaises(FeedFault):
            sequencer.consume(packet(3, system("Q", current)), current)
        with self.assertRaises(FeedFault):
            sequencer.consume(packet(2, system("Q", current)), current)

    def test_mid_session_or_historical_source_refused(self):
        sequencer, current = self.make()
        with self.assertRaises(FeedFault):
            sequencer.consume(packet(100, system("O", current)), current)
        sequencer, current = self.make()
        raw = bytearray(system("O", current))
        raw[5:11] = (0).to_bytes(6, "big")
        with self.assertRaises(FeedFault):
            sequencer.consume(packet(1, bytes(raw)), current)

    def test_conflicting_duplicate_refused(self):
        sequencer, current = self.make()
        sequencer.consume(packet(1, system("O", current)), current)
        with self.assertRaises(FeedFault):
            sequencer.consume(packet(1, system("Q", current)), current)

    def test_bad_frame_and_wrong_session(self):
        for raw in (b"bad", packet(1, system("O")) + b"unexpected", b"WRONGSESS1" + packet(1)[10:]):
            sequencer, current = self.make()
            with self.assertRaises(FeedFault):
                sequencer.consume(raw, current)


class AccountingTests(unittest.TestCase):
    def test_fifo_crossing_flat_and_fee_corrections(self):
        ledger = PnLLedger()
        ledger.fill("buy", 3, 10000, 100)
        ledger.fill("sell", -5, 10200, 100)
        self.assertEqual(ledger.position, -2)
        self.assertEqual(ledger.realized, 600)
        self.assertEqual(ledger.totals(10000, 10100), (600, 200, 200, 0))
        ledger.commission("buy", 50)
        ledger.commission("sell", -20)
        self.assertEqual(ledger.totals(10000, 10100), (600, 200, 50, 20))

    def test_unmatched_commission_does_not_fabricate_fill(self):
        with self.assertRaises(LedgerFault):
            PnLLedger().commission("unknown", 10)

    def test_token_bucket_bound_and_no_backward_clock(self):
        bucket = Bucket(2, 1, 100)
        bucket.take()
        self.assertFalse(bucket.available())
        bucket.refill(500_000_100)
        self.assertTrue(bucket.available())
        with self.assertRaises(SafetyFault):
            bucket.refill(0)

    def test_paper_attestations_and_live_rejection(self):
        config = settings(Path("/tmp/test-private-paper"))
        with patch.dict(os.environ, {"IBKR_EXECUTION_MODE": "live"}):
            with self.assertRaises(SafetyFault):
                config.validate(True)
        with patch.dict(os.environ, {"IBKR_EXECUTION_MODE": "paper", "IBKR_PAPER_ATTESTED": "false"}):
            with self.assertRaises(SafetyFault):
                config.validate(True)

    def test_quote_economics_bounds_and_integral_types(self):
        config = settings(Path("/tmp/test-private-paper"))
        with patch.dict(os.environ, {"IBKR_EXECUTION_MODE": "readonly"}):
            for field in ("fee_per_share_units", "minimum_fee_units", "slippage_buffer_units", "minimum_net_edge_units"):
                for bad in (-1, 1_000_000_001, 2**64, 1.5, True):
                    with self.subTest(field=field, bad=bad), self.assertRaises(SafetyFault):
                        replace(config, **{field: bad}).validate(False)
            with self.assertRaises(SafetyFault):
                replace(config, minimum_fee_units=0).validate(False)
            replace(config, fee_per_share_units=1_000_000_000, minimum_fee_units=1_000_000_000,
                    slippage_buffer_units=1_000_000_000, minimum_net_edge_units=1_000_000_000).validate(False)

    def test_quote_economics_environment_values(self):
        environment = {
            "IBKR_ACCOUNT": "DU123456", "TRADE_SYMBOL": "TEST", "IBKR_CON_ID": "123",
            "ITCH_STOCK_LOCATE": "1", "ITCH_GROUP": "239.1.1.1", "ITCH_PORT": "18000",
            "ITCH_INTERFACE_IP": "127.0.0.1", "ITCH_SOURCE_IP": "127.0.0.1",
            "ITCH_EXPECTED_SESSION": "TESTSESS01", "ITCH_SESSION_DATE": now_exchange().date().isoformat(),
            "IBKR_STATE_DIR": "/tmp/test-private-paper", "PAPER_FEE_PER_SHARE_UNITS": "50",
            "PAPER_MIN_FEE_UNITS": "5000", "PAPER_SLIPPAGE_BUFFER_UNITS": "125", "PAPER_MIN_NET_EDGE_UNITS": "250",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Settings.from_env()
            config.validate(False)
            self.assertEqual(config.quote_economics(), (50, 5000, 125, 250))
        with patch.dict(os.environ, {**environment, "PAPER_MIN_NET_EDGE_UNITS": "0.5"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_core_binding_requires_new_economics_export(self):
        class OlderLibrary:
            def __getattr__(self, name):
                if name == "vc_set_quote_economics":
                    raise AttributeError(name)
                return lambda *args: 0

        with tempfile.NamedTemporaryFile() as library:
            with patch("execution.core_binding.C.CDLL", return_value=OlderLibrary()):
                with self.assertRaisesRegex(CoreError, "ABI is incomplete"):
                    Core(Path(library.name), Config())

    def test_funds_and_ai_config_fail_closed(self):
        base = settings(Path("/tmp/test-private-paper"))
        changes = ({"allow_short": True}, {"funds_max_age_ms": 0}, {"funds_refresh_ms": 60_000},
                   {"ai_signal_ttl_ms": 0}, {"ai_signal_ttl_ms": 1001},
                   {"ai_min_expected_edge_bps": float("nan")}, {"ai_registry": Path("relative")})
        with patch.dict(os.environ, {"IBKR_EXECUTION_MODE": "readonly"}, clear=True):
            for change in changes:
                with self.subTest(change=change), self.assertRaises(SafetyFault):
                    replace(base, **change).validate(False)

    def test_journal_preserves_identity_and_blocks_same_day_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp), "DU123456", 71)
            try:
                journal.start_once("2026-11-01")
                order_id = journal.allocate_order(100, 0, 2, 10000)
                first, disposition = journal.record_execution("ABC.01.01", order_id, {"quantity": 1})
                self.assertEqual(disposition, "new")
                self.assertEqual(journal.record_execution("ABC.01.01", order_id, {"quantity": 1}), (first, "duplicate"))
                self.assertEqual(journal.record_execution("ABC.01.02", order_id, {"quantity": 2})[1], "correction")
                with self.assertRaises(LedgerFault):
                    journal.start_once("2026-11-01")
                self.assertEqual(journal.next_broker_id(1), 101)
            finally:
                journal.close()


class SDKCallbackContractTests(unittest.TestCase):
    def make_broker(self):
        class Wrapper:
            pass

        class Client:
            def __init__(self, wrapper):
                self.sent = []
                self.canceled = []

            def reqContractDetails(self, *args):
                pass

            def reqMarketRule(self, *args):
                pass

            def reqPositions(self, *args):
                pass

            def reqAllOpenOrders(self, *args):
                pass

            def reqExecutions(self, *args):
                pass

            def reqCurrentTime(self, *args):
                pass

            def reqAccountSummary(self, *args):
                pass

            def cancelAccountSummary(self, *args):
                pass

            def placeOrder(self, *args):
                self.sent.append(args)

            def cancelOrder(self, orderId, orderCancel):
                self.canceled.append((orderId, orderCancel))

        modules = {
            "ibapi.client": SimpleNamespace(EClient=Client),
            "ibapi.wrapper": SimpleNamespace(EWrapper=Wrapper),
            "ibapi.execution": SimpleNamespace(ExecutionFilter=SimpleNamespace),
            "ibapi.contract": SimpleNamespace(Contract=SimpleNamespace),
            "ibapi.order": SimpleNamespace(Order=SimpleNamespace),
            "ibapi.order_cancel": SimpleNamespace(OrderCancel=SimpleNamespace),
        }
        with patch("execution.auto_paper.importlib.import_module", side_effect=lambda name: modules[name]):
            broker = Broker(settings(Path("/tmp/private-test-only")))
        return broker, modules

    def test_new_and_old_error_signatures_normalized(self):
        broker, _ = self.make_broker()
        broker.client.error(10, 201, "rejected", "")
        self.assertEqual(broker.events.get_nowait(), ("error", {"request_id": 10, "code": 201}))
        broker.client.error(11, 123456789, 1100, "disconnected", "")
        self.assertEqual(broker.events.get_nowait(), ("error", {"request_id": 11, "code": 1100}))

    def test_modern_cancel_signature_and_no_constructor_send(self):
        broker, modules = self.make_broker()
        self.assertEqual(broker.client.sent, [])
        with patch("execution.auto_paper.importlib.import_module", side_effect=lambda name: modules[name]):
            broker.cancel(7)
        self.assertEqual(broker.client.canceled[0][0], 7)

    def test_commission_callback_variants_and_explicit_route(self):
        broker, _ = self.make_broker()
        broker.client.commissionAndFeesReport(SimpleNamespace(execId="E.1", commissionAndFees=0.25, currency="USD"))
        self.assertEqual(broker.events.get_nowait()[1]["fee"], "0.25")
        order = broker.build_order(0, 1, 10000)
        self.assertEqual(order.account, "DU123456")
        self.assertEqual(order.orderType, "LMT")
        self.assertFalse(order.outsideRth)
        self.assertEqual(broker.contract.exchange, "ISLAND")


class FakeBroker:
    """Pure test boundary; never imported by any production module."""
    def __init__(self):
        self.events = queue.Queue()
        self.requests = deque()
        self.overflow = threading.Event()
        self.sent = []
        self.canceled = []
        self.contract = SimpleNamespace(exchange="ISLAND")
        self.client = SimpleNamespace(isConnected=lambda: True, placeOrder=self.place_order,
                                      reqCurrentTime=lambda: None)

    def place_order(self, order_id, contract, order):
        self.sent.append((order_id, order))

    def build_order(self, side, quantity, price):
        return SimpleNamespace(side=side, quantity=quantity, price=price)

    def cancel(self, order_id):
        self.canceled.append(order_id)

    def request(self, name, *args):
        self.requests.append((name, args))


@unittest.skipUnless(LIBRARY.exists(), "Build the actual C++ core shared library before integration tests.")
class RealCorePaperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = settings(Path(self.temporary.name))
        self.core = Core(LIBRARY, self.config.core_config())
        self.core.set_quote_economics(*self.config.quote_economics())
        start = time.monotonic_ns() - 1_000_000
        for index, raw in enumerate((system("O"), system("Q"), status(), add(1, "B", 10000), add(2, "S", 10200))):
            self.core.process_itch(raw, start + index)
        self.core.health(broker=True, system=True, feed=True, sequence=True, live=True)
        self.core.call("update_pnl", 0, 0, 0, 0, 1)
        self.core.call("arm_flat", 1, 1, 0, 0, time.monotonic_ns())
        self.journal = Journal(Path(self.temporary.name), self.config.account, self.config.client_id)
        self.journal.start_once(self.config.session_date)
        self.broker = FakeBroker()
        self.runner = PaperRunner(self.config, self.core, self.broker, self.journal)
        self.runner.armed = True
        self.runner.next_id = 100
        self.runner.accounts_verified = True
        self.runner.directory_verified = True
        self.runner.feed.started = True
        self.runner.market_rules = [(0, 100)]
        self.runner.last_broker_time = time.monotonic_ns()
        self.runner.last_time_request = time.monotonic_ns()
        self.fund_snapshot("100000")

    def tearDown(self):
        self.journal.close()
        self.core.close()
        self.temporary.cleanup()

    def send(self):
        self.runner.send_order(0, 2, 10000, time.monotonic_ns())
        self.assertEqual(len(self.broker.sent), 1)
        return self.runner.orders[100]

    def fund_snapshot(self, value, request_id=10000):
        now = time.monotonic_ns()
        self.runner.resources.begin(request_id, now)
        for tag in ("AvailableFunds", "SettledCash", "BuyingPower"):
            self.runner.resources.receive(request_id, self.config.account, tag, value, "USD")
        self.assertTrue(self.runner.resources.complete(request_id, now + 1))
        self.runner.last_funds_request = now
        self.assertTrue(self.runner.account_gate_ready(time.monotonic_ns()))

    def report(self, execution_id="ABC.01.01", quantity="1", cumulative="1"):
        return dict(request_id=-1, account=self.config.account, con_id=self.config.con_id,
                    broker_id=100, client_id=self.config.client_id, perm_id=1000,
                    execution_id=execution_id, side="BOT", quantity=quantity,
                    price="1.0", cumulative=cumulative, broker_time="20261101 10:00:00")

    def status_report(self, **kwargs):
        return dict(broker_id=100, client_id=71, perm_id=1000,
                    status="Submitted", filled="0", remaining="2", **kwargs)

    def test_ctypes_layout_and_real_send_reservation(self):
        self.assertEqual(ctypes.sizeof(Health), 8)
        self.assertEqual(ctypes.sizeof(Permit), 24)
        self.assertGreater(ctypes.sizeof(Config), 100)
        self.assertGreater(ctypes.sizeof(Quote), 40)
        self.assertGreater(ctypes.sizeof(Snapshot), 40)
        self.send()
        self.assertEqual(self.core.snapshot().buy_reserved, 2)

    def enable_ai(self):
        from execution.tests.test_ai_gate import fixture_gate, warmup
        self.runner.s = replace(self.config, ai_registry=Path(self.temporary.name))
        gate, _, _ = fixture_gate(self.temporary.name, self.config.ai_policy_config())
        warmup(gate, time.monotonic_ns() - 100)
        self.runner.ai_gate = gate
        return gate

    def test_configured_ai_cannot_silently_fall_back_to_baseline(self):
        with self.assertRaisesRegex(SafetyFault, "verified participation gate"):
            PaperRunner(replace(self.config, ai_registry=Path(self.temporary.name)),
                        self.core, self.broker, self.journal)

    def test_ai_gate_cannot_change_cpp_quote_size_or_price(self):
        self.enable_ai()
        quote = self.core.quote(time.monotonic_ns())
        self.runner.send_order(0, quote.bid_quantity, quote.bid_price, time.monotonic_ns())
        self.assertEqual(len(self.broker.sent), 1)
        sent = self.broker.sent[0][1]
        self.assertEqual((sent.quantity, sent.price), (quote.bid_quantity, quote.bid_price))
        self.assertEqual(self.core.snapshot().buy_reserved, quote.bid_quantity)

    def test_ai_expiry_cancels_working_order_without_releasing_risk(self):
        gate = self.enable_ai()
        order = self.send()
        self.runner.last_quote = time.monotonic_ns()
        expiry = gate.last_signal["expires_ns"]
        self.runner.tick(expiry + 1)
        self.assertTrue(order.cancel_requested)
        self.assertIn(order.broker_id, self.broker.canceled)
        self.assertFalse(order.terminal)
        self.assertEqual(self.core.snapshot().buy_reserved, 2)
        self.assertEqual(len(self.broker.sent), 1)

    def test_ai_permission_does_not_bypass_missing_account_resources(self):
        self.enable_ai()
        self.runner.resources.invalidate("TEST_MISSING_FUNDS")
        self.runner.send_order(0, 2, 10000, time.monotonic_ns())
        self.assertFalse(self.broker.sent)
        self.assertEqual(self.core.snapshot().buy_reserved, 0)

    def test_ai_warmup_and_failure_cancel_old_quotes(self):
        gate = self.enable_ai()
        order = self.send()
        gate._sample_market = None
        self.runner.ai_participation(time.monotonic_ns())
        self.assertTrue(order.cancel_requested)
        self.assertEqual(self.core.snapshot().buy_reserved, 2)

    def test_expensive_ai_validation_cannot_outlive_cpp_permit(self):
        self.enable_ai()
        original = self.runner.ai_participation
        clock = time.monotonic_ns()
        calls = 0
        def delayed_check(now):
            nonlocal calls, clock
            result = original(now)
            calls += 1
            if calls == 2:
                # Simulated 150ms registry I/O, still inside the 250ms AI TTL,
                # but outside the 100ms C++ risk permit. No real sleep/network.
                clock += 150_000_000
            return result
        with patch("execution.auto_paper.time.monotonic_ns", side_effect=lambda: clock), \
                patch.object(self.runner, "ai_participation", side_effect=delayed_check):
            self.runner.send_order(0, 2, 10000, clock)
        self.assertEqual(calls, 2)
        self.assertFalse(self.broker.sent)
        self.assertEqual(self.core.snapshot().buy_reserved, 0)
        self.assertTrue(self.runner.orders[100].terminal)

    def test_per_message_feature_observation_uses_exchange_time_and_committed_sequence(self):
        gate = self.enable_ai()
        self.runner.feed.expected = 6
        first, second = add(3, "B", 10000), add(4, "S", 10200)
        with patch.object(gate, "observe") as observed:
            self.runner.process_packet(packet(6, first, second), self.config.source_ip, time.monotonic_ns())
        self.assertEqual(observed.call_count, 2)
        for index, call in enumerate(observed.call_args_list):
            self.assertEqual(call.kwargs["sequence"], 6 + index)
            self.assertEqual(call.kwargs["source_timestamp_ns"], int.from_bytes((first, second)[index][5:11], "big"))
        self.assertEqual(observed.call_args_list[0].kwargs["market"]["bid_qty"], 200)
        self.assertEqual(observed.call_args_list[0].kwargs["market"]["ask_qty"], 100)
        self.assertEqual(observed.call_args_list[1].kwargs["market"]["ask_qty"], 200)

    def test_armed_economics_cannot_change_or_reset_risk(self):
        before = self.core.snapshot()
        quote = self.core.quote(time.monotonic_ns())
        with self.assertRaises(CoreError):
            self.core.set_quote_economics(10, 100, 100, 100)
        after = self.core.snapshot()
        self.assertTrue(after.armed)
        self.assertEqual(after.risk_epoch, before.risk_epoch)
        self.assertFalse(after.killed)
        unchanged = self.core.quote(time.monotonic_ns())
        self.assertEqual((unchanged.bid_price, unchanged.ask_price), (quote.bid_price, quote.ask_price))

    def test_binding_prevents_integer_wrapping(self):
        for invalid in (-1, 1_000_000_001, 2**64, 1.5, True):
            with self.subTest(value=invalid), self.assertRaisesRegex(CoreError, "integer values"):
                self.core.set_quote_economics(invalid, 1, 0, 0)

    def test_economics_cannot_bypass_kill_or_open_reservation(self):
        self.send()
        self.core.call("kill", 9)
        before = self.core.snapshot()
        with self.assertRaises(CoreError):
            self.core.set_quote_economics(0, 1, 0, 0)
        after = self.core.snapshot()
        self.assertTrue(after.killed)
        self.assertFalse(after.armed)
        self.assertEqual(after.buy_reserved, before.buy_reserved)
        self.assertEqual(after.risk_epoch, before.risk_epoch)
        with self.assertRaises(CoreError):
            self.core.call("arm_flat", 1, 1, 0, 0, time.monotonic_ns())

    def test_unconfigured_economics_cannot_arm(self):
        fresh = Core(LIBRARY, self.config.core_config())
        try:
            start = time.monotonic_ns() - 1_000_000
            for index, raw in enumerate((system("O"), system("Q"), status(), add(1, "B", 10000), add(2, "S", 10200))):
                fresh.process_itch(raw, start + index)
            fresh.health(broker=True, system=True, feed=True, sequence=True, live=True)
            fresh.call("update_pnl", 0, 0, 0, 0, 1)
            self.assertFalse(fresh.quote(time.monotonic_ns()).quote_available)
            with self.assertRaises(CoreError):
                fresh.call("arm_flat", 1, 1, 0, 0, time.monotonic_ns())
            self.assertFalse(fresh.snapshot().armed)
            fresh.set_quote_economics(*self.config.quote_economics())
            fresh.call("arm_flat", 1, 1, 0, 0, time.monotonic_ns())
            self.assertTrue(fresh.snapshot().armed)
        finally:
            fresh.close()

    def test_real_core_cost_floor_and_rebate_independence(self):
        config = replace(self.config, price_high=2_000_000)
        fresh = Core(LIBRARY, config.core_config())
        try:
            fresh.set_quote_economics(50, 5000, 125, 250)
            start = time.monotonic_ns() - 1_000_000
            for index, raw in enumerate((system("O"), system("Q"), status(), add(1, "B", 1_000_000), add(2, "S", 1_000_200))):
                fresh.process_itch(raw, start + index)
            fresh.health(broker=True, system=True, feed=True, sequence=True, live=True)
            fresh.call("update_pnl", 0, 0, 0, 0, 1)
            quote = fresh.quote(time.monotonic_ns())
            self.assertTrue(quote.quote_available)
            half_cost = 50 + 5000 + 125 + 250
            midpoint = (quote.market_bid + quote.market_ask) // 2
            self.assertGreaterEqual(midpoint - quote.bid_price, half_cost)
            self.assertGreaterEqual(quote.ask_price - midpoint, half_cost)
            fresh.call("update_pnl", 0, 0, 0, 10000, 1)
            with_rebate = fresh.quote(time.monotonic_ns())
            self.assertEqual((quote.bid_price, quote.ask_price), (with_rebate.bid_price, with_rebate.ask_price))
        finally:
            fresh.close()

    def test_duplicate_fill_fee_adjustment_and_pending_cancel(self):
        order = self.send()
        self.runner.on_event("execution", self.report(), time.monotonic_ns())
        self.runner.on_event("execution", self.report(), time.monotonic_ns())
        self.assertEqual(self.core.snapshot().position, 1)
        self.assertEqual(self.core.snapshot().buy_reserved, 1)
        self.runner.commission({"execution_id": "ABC.01.01", "fee": "0.25", "currency": "USD"})
        self.assertEqual(self.core.snapshot().net_pnl_units, -2500)
        order.cancel_requested = True
        self.runner.drive_cancels(time.monotonic_ns())
        self.assertEqual(self.core.snapshot().buy_reserved, 1)
        cancel = {**self.status_report(), "status": "Cancelled", "filled": "1", "remaining": "1"}
        self.runner.on_event("order_status", cancel, time.monotonic_ns())
        self.assertEqual(self.core.snapshot().buy_reserved, 0)
        self.runner.on_event("execution", self.report("ABC.02.01", "1", "2"), time.monotonic_ns())
        self.assertEqual(self.core.snapshot().position, 2)
        self.assertTrue(self.runner.killed)

    def test_foreign_client_cancel_never_releases_reservation(self):
        self.send()
        foreign = {**self.status_report(), "client_id": 999, "status": "Cancelled"}
        with self.assertRaises(SafetyFault):
            self.runner.on_event("order_status", foreign, time.monotonic_ns())
        self.assertEqual(self.core.snapshot().buy_reserved, 2)

    def test_permanent_identity_change_refused(self):
        self.send()
        self.runner.on_event("order_status", self.status_report(), time.monotonic_ns())
        changed = {**self.status_report(), "perm_id": 2000, "status": "Cancelled"}
        with self.assertRaises(SafetyFault):
            self.runner.on_event("order_status", changed, time.monotonic_ns())
        self.assertEqual(self.core.snapshot().buy_reserved, 2)

    def test_pending_submit_can_have_unassigned_permanent_id(self):
        self.send()
        pending = {**self.status_report(), "perm_id": 0, "status": "PendingSubmit"}
        self.runner.on_event("order_status", pending, time.monotonic_ns())
        self.assertFalse(self.runner.orders[100].acknowledged)

    def test_sdk_send_exception_retains_exposure_and_never_retries(self):
        with patch.object(self.broker.client, "placeOrder", side_effect=OSError("connection reset")):
            self.runner.send_order(0, 2, 10000, time.monotonic_ns())
        self.assertTrue(self.runner.killed)
        self.assertEqual(self.core.snapshot().buy_reserved, 2)
        self.runner.send_order(0, 2, 10000, time.monotonic_ns())
        self.assertEqual(len(self.runner.orders), 1)

    def test_audit_disk_failure_does_not_skip_cancel(self):
        order = self.send()
        with patch.object(self.journal, "audit", side_effect=sqlite3.OperationalError("disk full")):
            self.runner.fault("test disk fault")
            self.runner.drive_cancels(time.monotonic_ns())
        self.assertTrue(order.cancel_requested)
        self.assertEqual(self.broker.canceled, [100])
        self.assertEqual(self.core.snapshot().buy_reserved, 2)

    def test_callback_backlog_blocks_send(self):
        self.broker.events.put(("execution", {}))
        self.runner.send_order(0, 2, 10000, time.monotonic_ns())
        self.assertFalse(self.broker.sent)
        self.assertEqual(self.core.snapshot().open_orders, 0)

    def test_reported_fill_without_execution_blocks_new_orders(self):
        self.send()
        report = {**self.status_report(), "status": "Filled", "filled": "2", "remaining": "0"}
        self.runner.on_event("order_status", report, time.monotonic_ns())
        self.runner.send_order(1, 2, 10200, time.monotonic_ns())
        self.assertEqual(len(self.broker.sent), 1)
        self.assertEqual(self.core.snapshot().position, 0)
        self.assertTrue(self.runner.pending_executions())

    def test_position_mismatch_blocks_send_and_cannot_extend_deadline(self):
        first = time.monotonic_ns()
        data = {"account": self.config.account, "con_id": self.config.con_id, "quantity": "1"}
        self.runner.on_event("position", data, first)
        self.runner.on_event("position", data, first + 1_000_000_000)
        self.assertEqual(self.runner.pending_positions[self.config.con_id][1], first)
        self.runner.send_order(0, 2, 10000, time.monotonic_ns())
        self.assertFalse(self.broker.sent)

    def test_terminal_order_missing_execution_still_times_out(self):
        order = self.send()
        cancel = {**self.status_report(), "status": "Cancelled"}
        self.runner.on_event("order_status", cancel, time.monotonic_ns())
        self.assertTrue(order.terminal)
        start = time.monotonic_ns()
        late_status = {**self.status_report(), "status": "Filled", "filled": "2", "remaining": "0"}
        self.runner.on_event("order_status", late_status, start)
        self.runner.tick(start + self.config.ack_timeout_ms * 1_000_000 + 1)
        self.assertTrue(self.runner.killed)
        self.assertTrue(order.unknown)

    def test_status_file_is_private_atomic_and_redacted(self):
        self.runner.status_file = Path(self.temporary.name) / "status.json"
        self.runner.write_status(time.monotonic_ns())
        data = json.loads(self.runner.status_file.read_text())
        self.assertEqual(data["kind"], "ibkr-paper")
        self.assertEqual(data["state"], "running")
        self.assertNotIn(self.config.account, self.runner.status_file.read_text())
        self.assertEqual(self.runner.status_file.stat().st_mode & 0o777, 0o600)

    def test_startup_control_requests_share_wire_budget(self):
        calls = []
        self.broker.client.reqPositions = lambda: calls.append("positions")
        for _ in range(5):
            self.broker.request("reqPositions")
        self.runner.drive_control_requests()
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.broker.requests), 2)
        self.runner.drive_control_requests()
        self.assertEqual(len(calls), 3)

    def test_missing_funds_blocks_order_without_reservation(self):
        self.runner.resources.invalidate("FUNDS_MISSING")
        self.runner.send_order(0, 2, 10000, time.monotonic_ns())
        self.assertFalse(self.broker.sent)
        self.assertEqual(self.core.snapshot().buy_reserved, 0)

    def test_unsettled_or_insufficient_funds_do_not_reuse_budget(self):
        self.fund_snapshot("3", request_id=10001)
        self.send()
        before = self.runner.resources.remaining(time.monotonic_ns(), self.runner.reserved_buy_resources())
        self.runner.send_order(0, 2, 10000, time.monotonic_ns())
        self.assertEqual(len(self.broker.sent), 1)
        self.runner.on_event("execution", self.report(), time.monotonic_ns())
        after = self.runner.resources.remaining(time.monotonic_ns(), self.runner.reserved_buy_resources())
        self.assertEqual(self.runner.resources.gross_buy_spend, 10000)
        self.assertEqual(before, after)
        self.fund_snapshot("100000", request_id=10002)
        self.assertEqual(after, self.runner.resources.remaining(time.monotonic_ns(), self.runner.reserved_buy_resources()))

    def test_short_order_denied_even_with_large_funds(self):
        self.runner.send_order(1, 2, 10200, time.monotonic_ns())
        self.assertFalse(self.broker.sent)
        self.assertEqual(self.core.snapshot().sell_reserved, 0)

    def test_stale_funds_arrange_cancel_without_releasing_reservation(self):
        order = self.send()
        later = self.runner.resources.snapshot.received_ns + self.config.funds_max_age_ms * 1_000_000 + 1
        self.assertFalse(self.runner.account_gate_ready(later))
        self.assertTrue(order.cancel_requested)
        self.assertEqual(self.core.snapshot().buy_reserved, 2)
        self.assertGreater(self.runner.reserved_buy_resources(), 0)


if __name__ == "__main__":
    unittest.main()
