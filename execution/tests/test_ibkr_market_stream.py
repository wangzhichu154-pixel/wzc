"""Callback contract tests with a fake SDK, not evidence of a broker connection."""
from contextlib import redirect_stdout
from dataclasses import asdict
from decimal import Decimal
import io
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from execution.ibkr_market_stream import Instrument, QuoteBuffer, StreamFault, main, stream_quotes
from execution.ibkr_probe import Configuration


ITEM = Instrument(123, "TEST", "NASDAQ")


class QuoteTests(unittest.TestCase):
    def push(self, buffer, **overrides):
        args = dict(req_id=1000, timestamp=100, bid=10, ask=11, bid_size=Decimal("1.5"),
                    ask_size=2, wall_ns=100_500_000_000, mono_ns=5_000_000_000)
        buffer.push(**(args | overrides))

    def test_exact_prices_fractional_sizes_and_honest_clock(self):
        buffer = QuoteBuffer([ITEM])
        self.push(buffer)
        quote = buffer.get(clock=lambda: 5_100_000_000)
        self.assertEqual((quote.bid_size, quote.source_unix_seconds), ("1.5", 100))
        self.assertFalse(quote.trading_authorized)
        self.assertFalse(quote.exchange_sequence_available)
        self.assertNotIn("timestamp_ns", asdict(quote))

    def test_two_changes_in_same_source_second_are_retained(self):
        buffer = QuoteBuffer([ITEM])
        self.push(buffer)
        self.push(buffer, bid=10.5, mono_ns=5_100_000_000)
        self.assertEqual(buffer.get(clock=lambda: 5_100_000_000).local_sequence, 1)
        self.assertEqual(buffer.get(clock=lambda: 5_100_000_000).local_sequence, 2)

    def test_consumer_clock_is_sampled_after_dequeue(self):
        buffer = QuoteBuffer([ITEM])
        self.push(buffer)

        def clock():
            self.assertTrue(buffer.queue.empty(), "clock sampled before dequeue")
            return 5_100_000_000

        self.assertIsNotNone(buffer.get(clock=clock))

    def test_each_invalid_quote_latches_failure(self):
        cases = [{"timestamp": 97}, {"timestamp": 102}, {"timestamp": True},
                 {"bid": "NaN"}, {"ask": float("inf")}, {"bid": 0}, {"bid_size": -1},
                 {"bid": 12}, {"bid": 11}, {"req_id": 42}, {"bid_past_low": True},
                 {"ask_past_high": True}]
        for changes in cases:
            with self.subTest(changes=changes):
                buffer = QuoteBuffer([ITEM])
                with self.assertRaises(StreamFault):
                    self.push(buffer, **changes)
                with self.assertRaises(StreamFault):
                    self.push(buffer)
                with self.assertRaises(StreamFault):
                    buffer.get(clock=lambda: 5_000_000_000)

    def test_backlog_expiration_uses_source_age_too(self):
        buffer = QuoteBuffer([ITEM])
        self.push(buffer)
        with self.assertRaisesRegex(StreamFault, "expired"):
            buffer.get(clock=lambda: 6_600_000_000)

    def test_clock_regression(self):
        for overrides in ({"mono_ns": 4_000_000_000}, {"timestamp": 99}, {"wall_ns": 100_400_000_000}):
            buffer = QuoteBuffer([ITEM])
            self.push(buffer)
            with self.assertRaisesRegex(StreamFault, "regressed"):
                self.push(buffer, **overrides)

    def test_full_buffer_never_silently_drops_data(self):
        buffer = QuoteBuffer([ITEM], capacity=1)
        self.push(buffer)
        with self.assertRaisesRegex(StreamFault, "overflow"):
            self.push(buffer)

    def test_universe_is_bounded_explicit_and_unique(self):
        for items in ([], [ITEM, ITEM], [Instrument(0, "TEST", "NASDAQ")],
                      [Instrument(123, "TEST", "")], [Instrument(True, "TEST", "NASDAQ")],
                      [Instrument(n + 1, f"S{n}", "NYSE") for n in range(11)]):
            with self.assertRaises(StreamFault):
                QuoteBuffer(items)

    def test_preflight_does_not_load_sdk_or_connect(self):
        with patch("execution.ibkr_market_stream.load_sdk", side_effect=AssertionError), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        self.assertIn('"network_started": false', output.getvalue())


class FakeClient:
    behavior = "ok"
    instance = None

    def __init__(self, wrapper):
        type(self).instance = self
        self.wrapper = wrapper
        self.stopped = threading.Event()
        self.cancelled = []
        self.requests = []

    def connect(self, host, port, clientId):
        self.wrapper.nextValidId(1)
        self.wrapper.managedAccounts("DU123" if self.behavior != "account" else "DU999")

    def isConnected(self):
        return True

    def run(self):
        self.stopped.wait(5)

    def disconnect(self):
        self.stopped.set()

    def reqContractDetails(self, req_id, contract):
        if self.behavior == "contract":
            contract.currency = "EUR"
        self.wrapper.contractDetails(req_id, SimpleNamespace(contract=contract))
        self.wrapper.contractDetailsEnd(req_id)

    def reqMarketDataType(self, kind):
        if self.behavior == "delayed":
            self.wrapper.marketDataType(1000, 3)

    def reqTickByTickData(self, req_id, contract, kind, history, ignore_size):
        self.requests.append((req_id, kind, history, ignore_size))
        if self.behavior == "entitlement":
            self.wrapper.error(req_id, 10189, "No tick-by-tick subscription", "")
        elif self.behavior == "new_error":
            self.wrapper.error(req_id, 123456, 10189, "No subscription", "")
        elif self.behavior == "disconnect":
            self.wrapper.connectionClosed()
        elif self.behavior != "silence":
            self.wrapper.tickByTickBidAsk(req_id, int(time.time()), 10, 11, 2, 3,
                                         SimpleNamespace(bidPastLow=False, askPastHigh=False))

    def cancelTickByTickData(self, req_id):
        self.cancelled.append(req_id)


class SDKContractTests(unittest.TestCase):
    def run_stream(self, behavior="ok", consumer=None):
        FakeClient.behavior = behavior
        # Client subclass is newly constructed for every call; inherited class
        # attributes and instance tracking belong to that dynamic subclass.
        def loader():
            class Wrapper:
                pass
            return FakeClient, Wrapper, object

        self.clients = []
        original_init = FakeClient.__init__

        def capture(client, wrapper):
            original_init(client, wrapper)
            self.clients.append(client)

        with patch("execution.ibkr_market_stream.load_sdk", loader), patch.object(FakeClient, "__init__", capture), patch(
                "execution.ibkr_market_stream.importlib.import_module", return_value=SimpleNamespace(Contract=SimpleNamespace)):
            return stream_quotes(Configuration(account="DU123", timeout=1), [ITEM], consumer or (lambda q: None), duration=1)

    def test_sdk_stream_delivers_in_memory_without_files_or_orders(self):
        quotes = []
        self.run_stream(consumer=quotes.append)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(self.clients[0].requests, [(1000, "BidAsk", 0, False)])
        self.assertEqual(self.clients[0].cancelled, [1000])
        self.assertTrue(self.clients[0].stopped.is_set())

    def test_identity_delay_entitlement_and_disconnect_fail_closed(self):
        for behavior in ("account", "contract", "delayed", "entitlement", "new_error", "disconnect", "silence"):
            with self.subTest(behavior=behavior), self.assertRaises(StreamFault):
                self.run_stream(behavior)
            self.assertTrue(self.clients[0].stopped.is_set())

    def test_consumer_failure_closes_subscription(self):
        def bad_consumer(quote):
            raise RuntimeError("consumer failed")
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            self.run_stream(consumer=bad_consumer)
        self.assertEqual(self.clients[0].cancelled, [1000])
        self.assertTrue(self.clients[0].stopped.is_set())


if __name__ == "__main__":
    unittest.main()
