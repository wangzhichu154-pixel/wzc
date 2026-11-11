"""Bounded IBKR BidAsk API stream. No historical input and no order methods.

This is the data-source foundation, not an execution permit. IBKR BBO is not
ITCH order-level data; its Unix-second timestamps cannot be fed to the existing
ITCH model or C++ engine. The consumer runs outside the SDK reader thread.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import importlib
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import threading
import time

from .ibkr_probe import Configuration, ProbeError, load_sdk


class StreamFault(RuntimeError):
    pass


@dataclass(frozen=True)
class Instrument:
    con_id: int
    symbol: str
    primary_exchange: str

    def validate(self):
        if type(self.con_id) is not int or not 0 < self.con_id < 2**31:
            raise StreamFault("An explicit qualified contract ID is required")
        if not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", self.symbol):
            raise StreamFault("Invalid stock symbol")
        if not isinstance(self.primary_exchange, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,15}", self.primary_exchange):
            raise StreamFault("Explicit primary exchange required")


@dataclass(frozen=True)
class Quote:
    symbol: str
    con_id: int
    bid: str
    ask: str
    bid_size: str
    ask_size: str
    source_unix_seconds: int
    received_wall_ns: int
    received_monotonic_ns: int
    local_sequence: int
    source: str = "IBKR_TICK_BY_TICK_BID_ASK"
    currency: str = "USD"
    exchange_sequence_available: bool = False
    trading_authorized: bool = False


class QuoteBuffer:
    """Single callback writer, bounded FIFO, latched failure; no lossy coalescing."""

    def __init__(self, instruments, *, capacity=4096, max_age_ms=2000):
        instruments = tuple(instruments)
        if not 1 <= len(instruments) <= 10:
            raise StreamFault("Explicit universe must contain 1..10 instruments; entitlement may allow fewer")
        for item in instruments:
            item.validate()
        if len({i.con_id for i in instruments}) != len(instruments) or len({i.symbol for i in instruments}) != len(instruments):
            raise StreamFault("Duplicate instrument")
        if type(capacity) is not int or not 1 <= capacity <= 65536:
            raise StreamFault("Invalid buffer capacity")
        if type(max_age_ms) is not int or not 1000 <= max_age_ms <= 10000:
            raise StreamFault("IBKR second-resolution source needs a 1000..10000 ms age bound")
        self.instruments = {1000 + n: item for n, item in enumerate(instruments)}
        self.queue = queue.Queue(maxsize=capacity)
        self.max_age_ns = max_age_ms * 1_000_000
        self.failed = threading.Event()
        self.reason = ""
        self.last = {}
        self.sequence = 0

    def fail(self, reason):
        if not self.failed.is_set():
            self.reason = reason
            self.failed.set()

    def check(self):
        if self.failed.is_set():
            raise StreamFault(self.reason)

    def push(self, req_id, timestamp, bid, ask, bid_size, ask_size, *, wall_ns, mono_ns,
             bid_past_low=False, ask_past_high=False):
        self.check()
        try:
            if req_id not in self.instruments:
                raise StreamFault("Unknown subscription ID")
            if type(timestamp) is not int or timestamp <= 0:
                raise StreamFault("Invalid source time")
            # Whole-second timestamps denote a bucket, not nanosecond precision.
            # Oldest possible source time is used conservatively for staleness.
            source_ns = timestamp * 1_000_000_000
            if source_ns > wall_ns + 250_000_000 or wall_ns - source_ns > self.max_age_ns:
                raise StreamFault("Stale or future source timestamp")
            previous = self.last.get(req_id)
            if previous and (timestamp < previous[0] or mono_ns < previous[1] or wall_ns < previous[2]):
                raise StreamFault("Source or local clock regressed")
            values = [Decimal(str(value)) for value in (bid, ask, bid_size, ask_size)]
            if any(not value.is_finite() or not 0 < value <= Decimal('1e12') for value in values):
                raise StreamFault("Invalid price or size")
            if values[0] >= values[1] or bid_past_low or ask_past_high:
                raise StreamFault("Locked/crossed or flagged quote is not usable")
            item = self.instruments[req_id]
            quote = Quote(item.symbol, item.con_id, *(str(v) for v in values), timestamp,
                          wall_ns, mono_ns, self.sequence + 1)
            self.queue.put_nowait(quote)
            self.sequence += 1
            self.last[req_id] = (timestamp, mono_ns, wall_ns)
        except (StreamFault, InvalidOperation, ValueError, queue.Full) as exc:
            self.fail(str(exc) or "Market queue overflow")
            raise StreamFault(self.reason) from exc

    def get(self, *, clock=time.monotonic_ns):
        self.check()
        try:
            quote = self.queue.get_nowait()
        except queue.Empty:
            return None
        # Sample after dequeue: the SDK may enqueue between the main loop's
        # earlier time sample and get_nowait. That is not clock regression.
        now_ns = clock()
        elapsed = now_ns - quote.received_monotonic_ns
        source_age = quote.received_wall_ns - quote.source_unix_seconds * 1_000_000_000
        if elapsed < 0 or elapsed + max(0, source_age) > self.max_age_ns:
            self.fail("Quote expired while awaiting consumer")
        self.check()
        return quote


def stream_quotes(config, instruments, consumer, *, duration=60, max_age_ms=2000):
    """Real SDK entry point; explicitly invoked by the operator, read-only only.

    No reconnect continuation: a disconnect invalidates the whole session. Future
    execution must reconcile positions/orders before enabling any new session.
    """
    config.validate()
    if not math.isfinite(config.timeout):
        raise StreamFault("Finite handshake timeout required")
    if config.account_kind != "paper":
        raise StreamFault("Initial integration accepts an explicitly identified Paper session only")
    if not math.isfinite(duration) or not 1 <= duration <= 3600:
        raise StreamFault("Duration must be 1..3600 seconds")
    buffer = QuoteBuffer(instruments, max_age_ms=max_age_ms)
    EClient, EWrapper, _ = load_sdk()
    Contract = importlib.import_module("ibapi.contract").Contract
    ready, accounts, stopping = threading.Event(), threading.Event(), threading.Event()
    qualified = {key: threading.Event() for key in buffer.instruments}
    matches = {key: [] for key in buffer.instruments}
    subscribed = []

    class Client(EWrapper, EClient):
        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self, self)

        def nextValidId(self, orderId):
            ready.set()

        def managedAccounts(self, accountsList):
            try:
                config.validate_managed_accounts(accountsList)
                accounts.set()
            except ProbeError:
                buffer.fail("Gateway account identity mismatch")

        def contractDetails(self, reqId, details):
            if reqId not in matches:
                buffer.fail("Unexpected contract response")
                return
            item, contract = buffer.instruments[reqId], details.contract
            if (contract.conId, contract.symbol, contract.secType, contract.currency, contract.primaryExchange) != (
                    item.con_id, item.symbol, "STK", "USD", item.primary_exchange):
                buffer.fail("Contract identity does not match configured USD stock")
                return
            if matches[reqId]:
                buffer.fail("Ambiguous contract qualification")
                return
            matches[reqId].append(contract)

        def contractDetailsEnd(self, reqId):
            if reqId not in matches or len(matches[reqId]) != 1:
                buffer.fail("Contract qualification incomplete")
            else:
                qualified[reqId].set()

        def marketDataType(self, reqId, marketDataType):
            if marketDataType != 1:
                buffer.fail("Delayed or frozen market data is forbidden")

        def tickByTickBidAsk(self, reqId, tick_time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk):
            try:
                buffer.push(reqId, tick_time, bidPrice, askPrice, bidSize, askSize,
                            wall_ns=time.time_ns(), mono_ns=time.monotonic_ns(),
                            bid_past_low=tickAttribBidAsk.bidPastLow, ask_past_high=tickAttribBidAsk.askPastHigh)
            except Exception:
                buffer.fail("Invalid BidAsk callback")

        def error(self, reqId, *args):
            if len(args) >= 3 and isinstance(args[1], int):
                code = args[1]
            elif len(args) >= 2:
                code = args[0]
            else:
                buffer.fail("Unknown SDK error callback")
                return
            # Only known farm status notices are informational. Entitlement,
            # pacing, reconnect and all unknown errors invalidate this session.
            if code not in {2104, 2106, 2107, 2108, 2158}:
                buffer.fail(f"IBKR feed error {code}; inspect Gateway diagnostics")

        def connectionClosed(self):
            if not stopping.is_set():
                buffer.fail("Gateway disconnected")

    client, reader = Client(), None
    previous_timeout = socket.getdefaulttimeout()

    def await_ready(events):
        deadline = time.monotonic() + config.timeout
        while not all(event.is_set() for event in events):
            buffer.check()
            if time.monotonic() >= deadline:
                raise StreamFault("Handshake or contract qualification timed out")
            buffer.failed.wait(0.01)
        buffer.check()

    def read():
        try:
            client.run()
        except Exception:
            buffer.fail("SDK reader failed")
        finally:
            if not stopping.is_set():
                buffer.fail("SDK reader stopped unexpectedly")

    try:
        socket.setdefaulttimeout(min(config.timeout, 5))
        try:
            client.connect(config.host, config.port, clientId=config.client_id)
        finally:
            socket.setdefaulttimeout(previous_timeout)
        if not client.isConnected():
            raise StreamFault("Gateway connection failed")
        reader = threading.Thread(target=read, name="ibkr-market-reader", daemon=True)
        reader.start()
        await_ready((ready, accounts))
        for req_id, item in buffer.instruments.items():
            contract = Contract()
            contract.conId, contract.symbol = item.con_id, item.symbol
            contract.secType, contract.currency = "STK", "USD"
            contract.exchange, contract.primaryExchange = "SMART", item.primary_exchange
            client.reqContractDetails(req_id, contract)
        await_ready(qualified.values())
        client.reqMarketDataType(1)  # Request only; not proof of subscription entitlement.
        started = time.monotonic_ns()
        for req_id in buffer.instruments:
            buffer.check()
            subscribed.append(req_id)
            # No historical ticks: numberOfTicks=0; include size-only changes.
            client.reqTickByTickData(req_id, matches[req_id][0], "BidAsk", 0, False)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            buffer.check()
            now = time.monotonic_ns()
            for req_id in subscribed:
                last = buffer.last.get(req_id)
                if now - (last[1] if last else started) > buffer.max_age_ns:
                    raise StreamFault("Missing or stale subscription; session invalidated")
            quote = buffer.get()
            if quote is not None:
                consumer(quote)
            else:
                buffer.failed.wait(0.005)
        buffer.check()
        if set(buffer.last) != set(subscribed):
            raise StreamFault("No fresh quote received for every subscription")
    finally:
        stopping.set()
        for req_id in subscribed:
            try:
                client.cancelTickByTickData(req_id)
            except Exception:
                buffer.fail("Subscription cancellation failed")
        try:
            client.disconnect()
        finally:
            if reader:
                reader.join(timeout=2)
                if reader.is_alive():
                    buffer.fail("SDK reader did not stop cleanly")
    buffer.check()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--universe", type=Path, help="Contract configuration, never historical market input")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if not args.connect:
        print(json.dumps({"network_started": False, "trading_enabled": False,
                          "source": "IBKR_TICK_BY_TICK_BID_ASK", "status": "NOT_CONNECTED"}))
        return 0
    try:
        if args.universe is None or args.universe.stat().st_size > 16384:
            raise StreamFault("A bounded universe configuration is required")
        raw = json.loads(args.universe.read_text())
        if not isinstance(raw, list):
            raise StreamFault("Universe must be an explicit instrument list")
        instruments = [Instrument(**entry) for entry in raw]
        config = Configuration(account=os.environ.get("IBKR_ACCOUNT", ""),
                               host=os.environ.get("IBKR_HOST", "127.0.0.1"),
                               port=int(os.environ.get("IBKR_PORT", "7497")),
                               client_id=int(os.environ.get("IBKR_MARKET_CLIENT_ID", "72")),
                               mode=os.environ.get("IBKR_EXECUTION_MODE", "readonly"))
        stream_quotes(config, instruments, lambda quote: print(json.dumps(asdict(quote)), flush=True),
                      duration=args.seconds)
    except (StreamFault, ProbeError, OSError, ValueError, TypeError, ImportError):
        print(json.dumps({"status": "FAILED", "trading_enabled": False,
                          "message": "Feed unavailable; verify configuration, SDK and Gateway diagnostics"}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
