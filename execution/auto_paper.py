"""Opt-in single-instrument IBKR paper executor. Never supports live orders.

Production code uses the official IBKR SDK and the C++ risk core. Neither
historical files nor Web requests can create order permits in this process.
Run without --enable-paper for a non-networking capability preflight only.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import ipaddress
import json
import math
import os
import queue
import select
import signal
import socket
import threading
import time
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from zoneinfo import ZoneInfo

from .core_binding import Config as CoreConfig, Core, CoreError
from .ai_gate import AIGateError, AIParticipationGate, market_from_quote
from .funds import AccountResources, FundsError, REQUIRED_TAGS, TradingPermissions
from .ibkr_probe import Configuration, ProbeError, load_sdk
from .live_feed import FeedFault, SessionSequencer, open_multicast
from .paper_ledger import Journal, LedgerFault, PnLLedger


class SafetyFault(RuntimeError):
    pass


def log(event: str, **values):
    print(json.dumps({"event": event, "utc_ns": time.time_ns(), **values}, allow_nan=False), flush=True)


def exact_units(value, *, positive=False) -> int:
    try:
        number = Decimal(str(value)) * 10_000
    except InvalidOperation as exc:
        raise SafetyFault("Invalid broker monetary field.") from exc
    if not number.is_finite() or number != number.to_integral_value() or abs(number) >= 2**62:
        raise SafetyFault("Broker price cannot be represented exactly in 1/10000 USD.")
    result = int(number)
    if positive and result <= 0:
        raise SafetyFault("Positive price required.")
    return result


def integral_quantity(value) -> int:
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value() or abs(number) > 2**31:
        raise SafetyFault("Fractional or invalid shares are unsupported.")
    return int(number)


@dataclass(frozen=True)
class Settings:
    account: str
    symbol: str
    con_id: int
    locate: int
    group: str
    port: int
    interface: str
    source_ip: str
    session: str
    session_date: str
    library: Path
    state_dir: Path
    host: str = "127.0.0.1"
    broker_port: int = 7497
    client_id: int = 71
    order_size: int = 1
    inventory_limit: int = 5
    notional_units: int = 100_000_000
    loss_units: int = 250_000
    drawdown_units: int = 250_000
    price_low: int = 10_000
    price_high: int = 10_000_000
    tick_units: int = 100
    market_age_ms: int = 1000
    source_age_ms: int = 1000
    ack_timeout_ms: int = 10_000
    quote_interval_ms: int = 500
    quote_lifetime_ms: int = 2000
    orders_per_second: int = 2
    cancels_per_second: int = 5
    messages_per_second: int = 10
    fee_per_share_units: int = 100
    minimum_fee_units: int = 10_000
    slippage_buffer_units: int = 100
    minimum_net_edge_units: int = 100
    funds_max_age_ms: int = 15000
    funds_refresh_ms: int = 5000
    stock_permission_attested: bool = False
    allow_short: bool = False
    ai_registry: Path | None = None
    ai_signal_ttl_ms: int = 250
    ai_min_expected_edge_bps: float = 1.0
    broker_timeout: float = 20.0

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.environ.get(name, "").strip()
            if not value:
                raise SafetyFault(f"Required configuration missing: {name}.")
            return value

        return cls(
            account=required("IBKR_ACCOUNT"), symbol=required("TRADE_SYMBOL"),
            con_id=int(required("IBKR_CON_ID")), locate=int(required("ITCH_STOCK_LOCATE")),
            group=required("ITCH_GROUP"), port=int(required("ITCH_PORT")),
            interface=required("ITCH_INTERFACE_IP"), source_ip=required("ITCH_SOURCE_IP"),
            session=os.environ.get("ITCH_EXPECTED_SESSION", ""), session_date=required("ITCH_SESSION_DATE"),
            library=Path(os.environ.get("VECTOR_CORE_LIBRARY", "build/libvector_core.so")),
            state_dir=Path(required("IBKR_STATE_DIR")),
            host=os.environ.get("IBKR_HOST", "127.0.0.1"),
            broker_port=int(os.environ.get("IBKR_PORT", "7497")),
            client_id=int(os.environ.get("IBKR_CLIENT_ID", "71")),
            stock_permission_attested=os.environ.get("IBKR_STOCK_TRADING_PERMISSION_ATTESTED") == "true",
            allow_short=os.environ.get("PAPER_ALLOW_SHORT", "false") != "false",
            ai_registry=Path(os.environ["PAPER_AI_REGISTRY"]) if os.environ.get("PAPER_AI_REGISTRY", "").strip() else None,
            ai_min_expected_edge_bps=float(os.environ.get("PAPER_AI_MIN_EXPECTED_EDGE_BPS", "1.0")),
            **{field: int(os.environ.get(env, default)) for field, env, default in (
                ("order_size", "PAPER_ORDER_SIZE", "1"),
                ("inventory_limit", "PAPER_INVENTORY_LIMIT", "5"),
                ("notional_units", "PAPER_MAX_NOTIONAL_UNITS", "100000000"),
                ("loss_units", "PAPER_LOSS_LIMIT_UNITS", "250000"),
                ("drawdown_units", "PAPER_DRAWDOWN_LIMIT_UNITS", "250000"),
                ("price_low", "PAPER_PRICE_LOW_UNITS", "10000"),
                ("price_high", "PAPER_PRICE_HIGH_UNITS", "10000000"),
                ("tick_units", "PAPER_TICK_UNITS", "100"),
                ("market_age_ms", "PAPER_MARKET_MAX_AGE_MS", "1000"),
                ("source_age_ms", "ITCH_SOURCE_MAX_AGE_MS", "1000"),
                ("ack_timeout_ms", "PAPER_ACK_TIMEOUT_MS", "10000"),
                ("quote_interval_ms", "PAPER_QUOTE_INTERVAL_MS", "500"),
                ("quote_lifetime_ms", "PAPER_QUOTE_LIFETIME_MS", "2000"),
                ("orders_per_second", "PAPER_ORDERS_PER_SECOND", "2"),
                ("cancels_per_second", "PAPER_CANCELS_PER_SECOND", "5"),
                ("messages_per_second", "PAPER_MESSAGES_PER_SECOND", "10"),
                ("fee_per_share_units", "PAPER_FEE_PER_SHARE_UNITS", "100"),
                ("minimum_fee_units", "PAPER_MIN_FEE_UNITS", "10000"),
                ("slippage_buffer_units", "PAPER_SLIPPAGE_BUFFER_UNITS", "100"),
                ("minimum_net_edge_units", "PAPER_MIN_NET_EDGE_UNITS", "100"),
                ("funds_max_age_ms", "PAPER_FUNDS_MAX_AGE_MS", "15000"),
                ("funds_refresh_ms", "PAPER_FUNDS_REFRESH_MS", "5000"),
                ("ai_signal_ttl_ms", "PAPER_AI_SIGNAL_TTL_MS", "250"),
            )},
        )

    def validate(self, enable: bool):
        Configuration(account=self.account, host=self.host, port=self.broker_port,
                      client_id=self.client_id, timeout=self.broker_timeout).validate()
        if os.environ.get("IBKR_EXECUTION_MODE", "readonly") not in {"readonly", "paper"}:
            raise SafetyFault("Live execution is not implemented and is always rejected.")
        if enable:
            if os.environ.get("IBKR_EXECUTION_MODE") != "paper":
                raise SafetyFault("Explicit IBKR_EXECUTION_MODE=paper is required.")
            for name in ("IBKR_PAPER_ATTESTED", "IBKR_EXCLUSIVE_ACCOUNT_ATTESTED",
                         "IBKR_DAY_START_ZERO_PNL_ATTESTED", "IBKR_STOCK_TRADING_PERMISSION_ATTESTED",
                         "ITCH_LIVE_SOURCE_ATTESTED"):
                if os.environ.get(name) != "true":
                    raise SafetyFault(f"Explicit operator attestation required: {name}=true.")
        if not self.symbol.isascii() or not 1 <= len(self.symbol) <= 8 or not self.symbol.replace(".", "").isalnum():
            raise SafetyFault("A single explicit ASCII stock symbol is required.")
        if not 1 <= self.locate <= 65535 or self.con_id <= 0:
            raise SafetyFault("Invalid stock locate or IBKR contract ID.")
        if len(self.session.encode("ascii")) != 10:
            raise SafetyFault("ITCH_EXPECTED_SESSION must contain exactly ten ASCII bytes.")
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if self.session_date != today:
            raise SafetyFault("Only the current exchange-date live session is permitted.")
        if not ipaddress.IPv4Address(self.group).is_multicast or not 1 <= self.port <= 65535:
            raise SafetyFault("Invalid multicast group or port.")
        ipaddress.IPv4Address(self.interface)
        ipaddress.IPv4Address(self.source_ip)
        if not 1 <= self.order_size <= 100 or not self.order_size <= self.inventory_limit <= 1000:
            raise SafetyFault("Paper quantity/inventory policy exceeded.")
        if not 0 < self.price_low < self.price_high <= 1_000_000_000 or self.tick_units <= 0:
            raise SafetyFault("Invalid configured price collar or tick size.")
        if any(value <= 0 or value >= 2**62 for value in (self.notional_units, self.loss_units, self.drawdown_units)):
            raise SafetyFault("Positive bounded monetary limits are required.")
        if not 10 <= self.market_age_ms <= 10_000 or not 10 <= self.source_age_ms <= 5000:
            raise SafetyFault("Market/source age must use a restrictive explicit bound.")
        if not 1000 <= self.ack_timeout_ms <= 60_000 or not 100 <= self.quote_interval_ms <= 10_000:
            raise SafetyFault("Invalid ACK timeout or quote interval.")
        if not self.quote_interval_ms <= self.quote_lifetime_ms <= 60_000:
            raise SafetyFault("Invalid quote lifetime.")
        if not 1 <= self.orders_per_second <= 5 or not 1 <= self.cancels_per_second <= 10:
            raise SafetyFault("Paper adapter action rate exceeds its supported envelope.")
        if not max(self.orders_per_second, self.cancels_per_second) < self.messages_per_second <= 20:
            raise SafetyFault("Shared message budget must reserve cancel capacity.")
        if any(type(value) is not int or not 0 <= value <= 1_000_000_000 for value in self.quote_economics()):
            raise SafetyFault("Quote economics require integer values in 0..1000000000 units.")
        if self.minimum_fee_units <= 0:
            raise SafetyFault("A conservative positive minimum commission estimate is required.")
        if self.allow_short:
            raise SafetyFault("Borrowing and short-sale permission verification is unsupported; Paper shorts are disabled.")
        if not 1000 <= self.funds_max_age_ms <= 60000 or not 1000 <= self.funds_refresh_ms <= self.funds_max_age_ms // 2:
            raise SafetyFault("Account resource refresh/freshness bounds are invalid.")
        if (type(self.ai_signal_ttl_ms) is not int or not 1 <= self.ai_signal_ttl_ms <= 1000
                or not math.isfinite(self.ai_min_expected_edge_bps)
                or not 0 <= self.ai_min_expected_edge_bps <= 10_000):
            raise SafetyFault("AI signal lifetime or expected edge bound is invalid.")
        if self.ai_registry is not None and not self.ai_registry.is_absolute():
            raise SafetyFault("AI registry must use an explicit operator-managed absolute path.")

    def ai_policy_config(self):
        from ml.runtime import PolicyConfig
        return PolicyConfig(signal_ttl_ns=self.ai_signal_ttl_ms * 1_000_000,
                            min_expected_edge_bps=self.ai_min_expected_edge_bps,
                            fee_per_share_units=self.fee_per_share_units,
                            min_fee_per_order_units=self.minimum_fee_units,
                            slippage_buffer_units=self.slippage_buffer_units,
                            max_target_position=self.inventory_limit, allow_short=False, clock="monotonic")

    def quote_economics(self):
        return (self.fee_per_share_units, self.minimum_fee_units,
                self.slippage_buffer_units, self.minimum_net_edge_units)

    def core_config(self):
        return CoreConfig(
            symbol_locate=self.locate, max_order_size=self.order_size,
            hard_inventory_limit=self.inventory_limit, max_notional_units=self.notional_units,
            max_open_orders=2, daily_loss_limit_units=self.loss_units,
            daily_drawdown_limit_units=self.drawdown_units, max_market_age_ns=self.market_age_ms * 1_000_000,
            orders_per_second=self.orders_per_second, price_low=self.price_low, price_high=self.price_high,
            tick_size=self.tick_units, max_book_orders=1_000_000, max_trade_records=1_000_000,
            symbol=self.symbol.encode("ascii"),
        )


class Bucket:
    def __init__(self, rate, burst, now):
        self.rate, self.capacity, self.credit, self.last = rate, burst * 1_000_000_000, burst * 1_000_000_000, now

    def refill(self, now):
        if now < self.last:
            raise SafetyFault("Monotonic clock moved backwards.")
        self.credit = min(self.capacity, self.credit + (now - self.last) * self.rate)
        self.last = now

    def available(self, cost=1):
        return self.credit >= cost * 1_000_000_000

    def take(self, cost=1):
        if not self.available(cost):
            raise SafetyFault("Unreserved message budget.")
        self.credit -= cost * 1_000_000_000


class Broker:
    """The official API is only touched by the owner thread, except its reader."""

    def __init__(self, settings: Settings):
        EClient, EWrapper, self.ExecutionFilter = load_sdk()
        self.Contract = importlib.import_module("ibapi.contract").Contract
        self.Order = importlib.import_module("ibapi.order").Order
        self.events: queue.Queue = queue.Queue(maxsize=10_000)
        self.overflow = threading.Event()
        self.requests = deque()
        self.settings = settings
        owner = self

        def emit(kind, **fields):
            try:
                owner.events.put_nowait((kind, fields))
            except queue.Full:
                owner.overflow.set()

        class Client(EWrapper, EClient):
            def __init__(self):
                EWrapper.__init__(self)
                EClient.__init__(self, self)

            def nextValidId(self, value):
                emit("next_id", value=value)

            def managedAccounts(self, value):
                emit("accounts", value=value)

            def currentTime(self, value):
                emit("time", value=value)

            def connectionClosed(self):
                emit("disconnect")

            def position(self, account, contract, position, avgCost):
                emit("position", account=account, con_id=contract.conId, quantity=str(position))

            def positionEnd(self):
                emit("positions_end")

            def accountSummary(self, reqId, account, tag, value, currency):
                emit("account_summary", request_id=reqId, account=account, tag=tag, value=str(value), currency=currency)

            def accountSummaryEnd(self, reqId):
                emit("account_summary_end", request_id=reqId)

            def openOrder(self, orderId, contract, order, orderState):
                emit("open_order", account=order.account, broker_id=orderId, con_id=contract.conId,
                     client_id=order.clientId, perm_id=order.permId, status=orderState.status)

            def openOrderEnd(self):
                emit("orders_end")

            def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId,
                            parentId, lastFillPrice, clientId, whyHeld, mktCapPrice=0):
                emit("order_status", broker_id=orderId, status=status, filled=str(filled),
                     remaining=str(remaining), client_id=clientId, perm_id=permId)

            def execDetails(self, reqId, contract, execution):
                emit("execution", request_id=reqId, account=execution.acctNumber,
                     con_id=contract.conId, broker_id=execution.orderId, client_id=execution.clientId,
                     perm_id=execution.permId, execution_id=execution.execId,
                     side=execution.side, quantity=str(execution.shares), price=str(execution.price),
                     cumulative=str(execution.cumQty), broker_time=execution.time)

            def execDetailsEnd(self, reqId):
                if reqId == 9002:
                    emit("executions_end")

            def commissionReport(self, report):
                emit("commission", execution_id=report.execId, fee=str(report.commission), currency=report.currency)

            def commissionAndFeesReport(self, report):
                emit("commission", execution_id=report.execId, fee=str(report.commissionAndFees), currency=report.currency)

            def contractDetails(self, reqId, details):
                if reqId == 9003:
                    contract = details.contract
                    emit("contract", con_id=contract.conId, symbol=contract.symbol,
                         currency=contract.currency, security_type=contract.secType,
                         exchanges=details.validExchanges, rules=details.marketRuleIds)

            def contractDetailsEnd(self, reqId):
                if reqId == 9003:
                    emit("contract_end")

            def marketRule(self, marketRuleId, priceIncrements):
                emit("market_rule", rule_id=marketRuleId,
                     increments=[(str(item.lowEdge), str(item.increment)) for item in priceIncrements])

            def error(self, reqId, *args):
                if len(args) >= 3 and isinstance(args[1], int):
                    code = args[1]
                elif len(args) >= 2:
                    code = args[0]
                else:
                    emit("sdk_failure")
                    return
                emit("error", request_id=reqId, code=code)

        self.client = Client()
        self.reader = None
        self.contract = self.Contract()
        self.contract.conId = settings.con_id
        self.contract.symbol = settings.symbol
        self.contract.secType = "STK"
        self.contract.currency = "USD"
        self.contract.exchange = "ISLAND"
        self.contract.primaryExchange = "NASDAQ"
        required = ("reqContractDetails", "reqMarketRule", "reqPositions", "reqAllOpenOrders",
                    "reqExecutions", "placeOrder", "cancelOrder", "reqCurrentTime",
                    "reqAccountSummary", "cancelAccountSummary")
        if any(not callable(getattr(self.client, name, None)) for name in required):
            raise SafetyFault("Official SDK lacks required contract, market-rule, or order capabilities.")

    def start(self):
        previous = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(5)
            self.client.connect(self.settings.host, self.settings.broker_port, clientId=self.settings.client_id)
        finally:
            socket.setdefaulttimeout(previous)
        if not self.client.isConnected():
            raise SafetyFault("IBKR connection was not established.")

        def run():
            try:
                self.client.run()
            except Exception:
                self.overflow.set()

        self.reader = threading.Thread(target=run, name="official-ibkr-reader", daemon=True)
        self.reader.start()

    def reconcile(self):
        self.request("reqPositions")
        self.request("reqAllOpenOrders")
        execution_filter = self.ExecutionFilter()
        execution_filter.acctCode = self.settings.account
        self.request("reqExecutions", 9002, execution_filter)

    def request(self, name, *args):
        if name not in {"reqPositions", "reqAllOpenOrders", "reqExecutions", "reqCurrentTime",
                        "reqContractDetails", "reqMarketRule", "reqAccountSummary", "cancelAccountSummary"} or len(self.requests) >= 100:
            raise SafetyFault("Unsupported or excessive broker control requests.")
        self.requests.append((name, args))

    def build_order(self, side: int, quantity: int, price: int):
        order = self.Order()
        order.account = self.settings.account
        order.action = "BUY" if side == 0 else "SELL"
        order.orderType = "LMT"
        order.totalQuantity = Decimal(quantity)
        order.lmtPrice = float(Decimal(price) / 10_000)
        order.tif = "DAY"
        order.outsideRth = False
        order.transmit = True
        order.orderRef = "VECTOR-PAPER-V1"
        # Forbid silent routing through SMART; the Contract pins ISLAND.
        if self.contract.exchange != "ISLAND" or order.account != self.settings.account:
            raise SafetyFault("Order routing or account changed.")
        return order

    def cancel(self, broker_id):
        parameters = inspect.signature(self.client.cancelOrder).parameters
        if "orderCancel" in parameters:
            OrderCancel = importlib.import_module("ibapi.order_cancel").OrderCancel
            self.client.cancelOrder(broker_id, OrderCancel())
        elif "manualCancelOrderTime" in parameters:
            self.client.cancelOrder(broker_id, "")
        elif len(parameters) == 1:
            self.client.cancelOrder(broker_id)
        else:
            raise SafetyFault("Unsupported official SDK cancelOrder signature.")

    def close(self):
        self.client.disconnect()
        if self.reader:
            self.reader.join(timeout=2)


@dataclass
class WorkingOrder:
    internal_id: int
    broker_id: int
    side: int
    quantity: int
    price: int
    created_ns: int
    filled: int = 0
    acknowledged: bool = False
    cancel_requested: bool = False
    cancel_sent_ns: int = 0
    terminal: bool = False
    cancel_ack_filled: int | None = None
    unknown: bool = False
    perm_id: int = 0
    reported_filled: int = 0
    unmatched_fill_since_ns: int = 0


class PaperRunner:
    def __init__(self, settings: Settings, core: Core, broker: Broker, journal: Journal,
                 status_file: Path | None = None, ai_gate: AIParticipationGate | None = None):
        self.s, self.core, self.broker, self.journal = settings, core, broker, journal
        if bool(settings.ai_registry) != bool(ai_gate):
            raise SafetyFault("Configured AI mode requires its verified participation gate.")
        self.ai_gate = ai_gate
        self.feed = SessionSequencer(settings.session.encode("ascii"), settings.session_date,
                                     settings.source_age_ms * 1_000_000)
        self.pnl = PnLLedger()
        self.resources = AccountResources(settings.account, settings.funds_max_age_ms * 1_000_000)
        self.permissions = TradingPermissions(settings.account, settings.stock_permission_attested)
        self.orders: dict[int, WorkingOrder] = {}
        self.commissions: dict[str, int] = {}
        self.reconciled_flags: set[str] = set()
        self.contracts = []
        self.market_rules: list[tuple[int, int]] = []
        self.rule_id = None
        self.next_id = None
        self.accounts_verified = False
        self.directory_verified = False
        self.armed = False
        self.killed = False
        self.reason = ""
        self.reconcile_at = 0
        self.reconcile_started = 0
        self.last_broker_time = 0
        self.last_time_request = 0
        self.last_quote = 0
        self.last_feed_packet = 0
        self.last_status = 0
        self.shutdown_deadline = 0
        self.status_file = status_file
        self.last_status_file = 0
        self.last_funds_request = 0
        self.next_funds_request_id = 1_500_000_000
        self.funds_gate_reason = "FUNDS_MISSING"
        self.pending_positions: dict[int, tuple[int, int]] = {}
        now = time.monotonic_ns()
        self.started = now
        self.new_bucket = Bucket(settings.orders_per_second, 2, now)
        self.cancel_bucket = Bucket(settings.cancels_per_second, 2, now)
        self.wire_bucket = Bucket(settings.messages_per_second, 3, now)

    def fault(self, message: str, reason=9):
        # This must happen before any disk, log, or ABI operation that can fail.
        for order in self.orders.values():
            if not order.terminal:
                order.cancel_requested = True
        if not self.killed:
            self.killed = True
            self.reason = message
            try:
                self.core.call("kill", reason)
            except Exception:
                # Owner-level latch still forbids sends if the C ABI itself fails.
                pass
            try:
                self.journal.audit("kill", {"reason": message})
            except Exception:
                pass
            try:
                log("paper_killed", reason=message, cancellation_confirmed=False)
            except Exception:
                pass

    def unknown(self, order: WorkingOrder, reason: str):
        order.unknown = True
        order.cancel_requested = True
        self.fault(reason, 4)
        self.core.call("outcome_unknown", order.internal_id, accepted=(0, 5))
        self.journal.order_state(order.internal_id, "unknown")

    def bind_identity(self, order, data, *, allow_unassigned=False):
        if data["client_id"] != self.s.client_id:
            raise SafetyFault("Broker callback client or permanent identity is not verified.")
        permanent = int(data["perm_id"])
        if permanent == 0 and allow_unassigned:
            return
        if permanent <= 0:
            raise SafetyFault("Permanent broker order identity is not verified.")
        if order.perm_id and order.perm_id != permanent:
            raise SafetyFault("Broker permanent order identity changed.")
        order.perm_id = permanent

    def begin_reconciliation(self, now):
        self.reconciled_flags.clear()
        self.reconcile_at = 0
        self.reconcile_started = now
        self.broker.reconcile()

    def begin_funds_query(self, now):
        if self.resources.pending_request is not None:
            return
        while self.next_funds_request_id in self.orders:
            self.next_funds_request_id += 1
        if self.next_funds_request_id >= 2_147_483_647:
            raise SafetyFault("Account summary request-ID capacity exhausted.")
        request_id = self.next_funds_request_id
        self.next_funds_request_id += 1
        self.resources.begin(request_id, now)
        self.last_funds_request = now
        self.broker.request("reqAccountSummary", request_id, "All", ",".join(sorted(REQUIRED_TAGS)))

    def pause_increasing_quotes(self, reason):
        self.funds_gate_reason = reason
        snapshot = self.core.snapshot()
        for order in self.orders.values():
            if not order.terminal and (order.side == 0 or snapshot.position <= 0
                                       or snapshot.sell_reserved > snapshot.position):
                order.cancel_requested = True

    def reserved_buy_resources(self):
        per_share_buffer = self.s.fee_per_share_units + self.s.minimum_fee_units + self.s.slippage_buffer_units
        return sum((order.quantity - order.filled) * (order.price + per_share_buffer)
                   for order in self.orders.values()
                   if order.side == 0 and (not order.terminal or order.unknown))

    def account_gate_ready(self, now):
        if not self.permissions.may_buy(self.s.account):
            self.pause_increasing_quotes("STOCK_PERMISSION_UNVERIFIED")
            return False
        available = self.resources.remaining(now, self.reserved_buy_resources())
        if available is None:
            self.pause_increasing_quotes(self.resources.invalid_reason or "FUNDS_STALE")
            return False
        if available < 0:
            self.pause_increasing_quotes("FUNDS_COMMITMENTS_EXCEED_BUDGET")
            return False
        self.funds_gate_reason = ""
        return True

    def order_account_gate(self, side, quantity, price, now, *, already_reserved=False):
        if not self.account_gate_ready(now):
            return False
        snapshot = self.core.snapshot()
        if side == 1:
            reserved = snapshot.sell_reserved - (quantity if already_reserved else 0)
            return self.permissions.may_sell(self.s.account, position=snapshot.position,
                                             reserved_sells=reserved, quantity=quantity)
        additional = 0 if already_reserved else quantity * (
            price + self.s.fee_per_share_units + self.s.minimum_fee_units + self.s.slippage_buffer_units
        )
        return self.resources.can_reserve_buy(now, self.reserved_buy_resources(), additional)

    def on_event(self, kind, data, now):
        if kind == "accounts":
            Configuration(account=self.s.account).validate_managed_accounts(data["value"])
            self.accounts_verified = True
        elif kind == "next_id":
            if self.next_id is None:
                if not 1 <= int(data["value"]) < 2_147_483_647:
                    raise SafetyFault("Invalid broker order-ID handshake.")
                self.next_id = self.journal.next_broker_id(int(data["value"]))
                self.broker.request("reqCurrentTime")
                self.last_time_request = now
                self.broker.request("reqContractDetails", 9003, self.broker.contract)
                self.begin_reconciliation(now)
                self.begin_funds_query(now)
        elif kind == "time":
            if abs(time.time() - int(data["value"])) > 5:
                raise SafetyFault("Broker and local wall clocks differ by more than five seconds.")
            self.last_broker_time = now
        elif kind == "account_summary":
            try:
                self.resources.receive(data["request_id"], data["account"], data["tag"], data["value"], data["currency"])
            except FundsError:
                request_id = self.resources.pending_request
                if request_id is not None:
                    self.resources.abandon(request_id, "FUNDS_INVALID")
                    self.broker.request("cancelAccountSummary", request_id)
                self.pause_increasing_quotes("FUNDS_INVALID")
        elif kind == "account_summary_end":
            if data["request_id"] == self.resources.pending_request:
                accepted = self.resources.complete(data["request_id"], now)
                self.broker.request("cancelAccountSummary", data["request_id"])
                if not accepted:
                    self.pause_increasing_quotes(self.resources.invalid_reason)
        elif kind in {"disconnect", "sdk_failure"}:
            for order in self.orders.values():
                if not order.terminal:
                    self.unknown(order, "Broker disconnected with unresolved orders.")
            self.fault("Broker disconnected or SDK failed.", 3)
        elif kind == "error":
            if data["request_id"] == self.resources.pending_request:
                request_id = self.resources.pending_request
                self.resources.abandon(request_id, "FUNDS_QUERY_REJECTED")
                self.broker.request("cancelAccountSummary", request_id)
                self.pause_increasing_quotes("FUNDS_QUERY_REJECTED")
                return
            if data["code"] not in {2104, 2106, 2107, 2108, 2158, 202}:
                order = self.orders.get(data["request_id"])
                if order and not order.terminal:
                    self.unknown(order, f"Broker order error {data['code']} requires reconciliation.")
                self.fault(f"IBKR error code {data['code']}.", 3)
        elif kind == "contract":
            self.contracts.append(data)
            if len(self.contracts) > 1:
                raise SafetyFault("Ambiguous broker contract.")
        elif kind == "contract_end":
            if len(self.contracts) != 1:
                raise SafetyFault("Broker did not resolve exactly one contract.")
            details = self.contracts[0]
            if (details["con_id"], details["symbol"], details["currency"], details["security_type"]) != (
                self.s.con_id, self.s.symbol, "USD", "STK"
            ):
                raise SafetyFault("Broker contract differs from configured instrument.")
            exchanges, rules = details["exchanges"].split(","), details["rules"].split(",")
            if "ISLAND" not in exchanges or len(exchanges) != len(rules):
                raise SafetyFault("Direct ISLAND route with market-rule metadata is required.")
            self.rule_id = int(rules[exchanges.index("ISLAND")])
            if self.rule_id <= 0:
                raise SafetyFault("Valid broker market rule is required.")
            self.broker.request("reqMarketRule", self.rule_id)
        elif kind == "market_rule":
            if data["rule_id"] != self.rule_id or not data["increments"]:
                raise SafetyFault("Unexpected or empty market rule.")
            self.market_rules = sorted((exact_units(edge), exact_units(step, positive=True))
                                       for edge, step in data["increments"])
        elif kind == "position" and data["account"] == self.s.account:
            quantity = integral_quantity(data["quantity"])
            if not self.armed and quantity:
                raise SafetyFault("Paper startup requires the entire dedicated account to be flat.")
            if self.armed:
                if data["con_id"] != self.s.con_id and quantity:
                    raise SafetyFault("Unexpected position in dedicated account.")
                if data["con_id"] == self.s.con_id:
                    old = self.pending_positions.get(data["con_id"])
                    first_mismatch = old[1] if old and old[1] else now
                    self.pending_positions[data["con_id"]] = (
                        quantity, first_mismatch if quantity != self.pnl.position else 0
                    )
        elif kind in {"positions_end", "orders_end", "executions_end"}:
            self.reconciled_flags.add(kind)
            if self.reconciled_flags == {"positions_end", "orders_end", "executions_end"}:
                self.reconcile_at = now
        elif kind == "open_order":
            if not data["account"]:
                raise SafetyFault("Open order has ambiguous account ownership.")
            if data["account"] == self.s.account:
                if not self.armed:
                    raise SafetyFault("Paper startup requires zero open orders on the account.")
                order = self.orders.get(data["broker_id"])
                if not order or data["client_id"] != self.s.client_id or data["con_id"] != self.s.con_id:
                    raise SafetyFault("Foreign order detected on dedicated account.")
                self.bind_identity(order, data, allow_unassigned=data["status"] in {"PendingSubmit", "ApiPending"})
                self.journal.audit("broker_identity", data)
                if data["status"] in {"Submitted", "PreSubmitted"}:
                    self.ack(order)
        elif kind == "order_status" and self.armed:
            order = self.orders.get(data["broker_id"])
            if not order:
                if data["client_id"] == self.s.client_id:
                    raise SafetyFault("Unknown order status for our client ID.")
                return
            self.bind_identity(order, data, allow_unassigned=data["status"] in {"PendingSubmit", "ApiPending"})
            cumulative = integral_quantity(data["filled"])
            remaining = integral_quantity(data["remaining"])
            if not 0 <= cumulative <= order.quantity or not 0 <= remaining <= order.quantity:
                raise SafetyFault("Invalid broker cumulative quantity.")
            self.note_execution_watermark(order, cumulative, now)
            if data["status"] == "Filled" and (cumulative != order.quantity or remaining != 0):
                raise SafetyFault("Filled status contradicts original order quantity.")
            if data["status"] in {"Submitted", "PreSubmitted"}:
                self.ack(order)
            elif data["status"] in {"Cancelled", "ApiCancelled"}:
                order.cancel_ack_filled = cumulative
                self.complete_cancel(order)
            elif data["status"] == "Inactive":
                self.unknown(order, "Inactive order status needs reconciliation.")
            # Filled status never substitutes for an execution report.
        elif kind == "execution" and data["account"] == self.s.account:
            if not self.armed:
                raise SafetyFault("Existing executions prevent a zero-PnL startup; history reconciliation is unsupported.")
            self.execution(data)
        elif kind == "commission":
            self.commission(data)

    def ack(self, order):
        if not order.acknowledged and not order.terminal:
            self.core.call("ack", order.internal_id)
            order.acknowledged = True
            self.journal.order_state(order.internal_id, "acknowledged")

    def note_execution_watermark(self, order, cumulative, now):
        order.reported_filled = max(order.reported_filled, cumulative)
        if order.reported_filled > order.filled and not order.unmatched_fill_since_ns:
            order.unmatched_fill_since_ns = now
        elif order.reported_filled <= order.filled:
            order.unmatched_fill_since_ns = 0

    def pending_executions(self):
        return any(order.reported_filled > order.filled for order in self.orders.values())

    def broker_ledger_pending(self):
        return self.pending_executions() or any(
            quantity != (self.pnl.position if con_id == self.s.con_id else 0)
            for con_id, (quantity, _) in self.pending_positions.items()
        )

    def complete_cancel(self, order):
        if order.cancel_ack_filled is None or order.terminal:
            return
        if order.cancel_ack_filled < order.filled:
            raise SafetyFault("Cancel status conflicts with known executions.")
        if order.cancel_ack_filled == order.filled:
            self.core.call("cancel_confirm", order.internal_id)
            order.terminal = True
            self.journal.order_state(order.internal_id, "canceled")

    def execution(self, data):
        order = self.orders.get(data["broker_id"])
        if not order or data["client_id"] != self.s.client_id or data["con_id"] != self.s.con_id:
            raise SafetyFault("Unknown execution requires broker reconciliation.")
        self.bind_identity(order, data)
        quantity, price = integral_quantity(data["quantity"]), exact_units(data["price"], positive=True)
        if quantity <= 0 or data["side"] != ("BOT" if order.side == 0 else "SLD"):
            raise SafetyFault("Execution side or quantity conflicts with order.")
        cumulative = integral_quantity(data["cumulative"])
        if not quantity <= cumulative <= order.quantity:
            raise SafetyFault("Execution cumulative quantity is inconsistent.")
        payload = {key: value for key, value in data.items() if key != "request_id"}
        key, disposition = self.journal.record_execution(data["execution_id"], order.internal_id, payload)
        if disposition == "duplicate":
            return
        if disposition == "correction":
            self.core.call("execution_correction", accepted=(0, 5))
            self.fault("Execution correction requires offline ledger reconciliation.", 5)
            return
        if quantity > order.quantity - order.filled:
            raise SafetyFault("Execution exceeds original order quantity.")
        was_terminal = order.terminal
        self.core.call("fill", key, order.internal_id, quantity, price)
        fee = max(self.s.minimum_fee_units, quantity * self.s.fee_per_share_units)
        self.pnl.fill(data["execution_id"], quantity if order.side == 0 else -quantity, price, fee)
        if order.side == 0:
            self.resources.record_buy_fill(quantity, price)
        if data["execution_id"] in self.commissions:
            self.pnl.commission(data["execution_id"], self.commissions.pop(data["execution_id"]))
        order.filled += quantity
        self.note_execution_watermark(order, cumulative, time.monotonic_ns())
        if order.filled == order.quantity:
            order.terminal = True
            self.journal.order_state(order.internal_id, "filled")
        elif not order.terminal:
            self.complete_cancel(order)
        if was_terminal:
            self.fault("Late fill arrived after a terminal cancel.", 4)
        self.mark(time.monotonic_ns())

    def commission(self, data):
        if data["currency"] != "USD":
            raise SafetyFault("Non-USD commission requires unsupported FX valuation.")
        value = Decimal(data["fee"])
        if not value.is_finite() or abs(value) > 1_000_000:
            raise SafetyFault("Invalid commission report.")
        # Round positive fees up / negative rebates toward zero, conservatively.
        units = int((value * 10_000).to_integral_value(rounding=ROUND_CEILING))
        self.journal.audit("commission", {"execution_id": data["execution_id"], "units": units})
        if data["execution_id"] in self.pnl.fees:
            self.pnl.commission(data["execution_id"], units)
            self.mark(time.monotonic_ns())
        else:
            if len(self.commissions) >= 10_000:
                raise SafetyFault("Unmatched commissions exceeded capacity.")
            self.commissions[data["execution_id"]] = units

    def mark(self, now):
        self.resources.observe_positive_fees(sum(max(value, 0) for value in self.pnl.fees.values()))
        quote = self.core.quote(now)
        valid = bool(quote.market_fresh and quote.market_bid > 0 and quote.market_ask > quote.market_bid)
        if self.pnl.position and not valid:
            self.core.call("update_pnl", self.pnl.realized, 0,
                           sum(max(value, 0) for value in self.pnl.fees.values()),
                           sum(max(-value, 0) for value in self.pnl.fees.values()), 0, accepted=(0, 5))
            self.fault("Open inventory has no fresh liquidation mark.", 2)
            return
        totals = self.pnl.totals(quote.market_bid, quote.market_ask)
        self.core.call("update_pnl", *totals, int(valid), accepted=(0, 5))
        snapshot = self.core.snapshot()
        if snapshot.position != self.pnl.position:
            raise SafetyFault("C++ and FIFO positions disagree.")
        if snapshot.killed and not self.killed:
            self.fault("C++ loss, drawdown, or risk latch triggered.", 8)

    def process_packet(self, packet, source, now):
        if source != self.s.source_ip:
            return
        messages = self.feed.consume(packet)
        self.last_feed_packet = now
        first_sequence = self.feed.expected - len(messages)
        for offset, raw in enumerate(messages):
            if raw[0:1] == b"R" and int.from_bytes(raw[1:3], "big") == self.s.locate:
                if len(raw) < 19 or raw[11:19].decode("ascii").strip() != self.s.symbol:
                    raise FeedFault("Stock directory differs from configured symbol/locate.")
                self.directory_verified = True
            self.core.process_itch(raw, now)
            if self.ai_gate and (raw[0:1] == b"S" or int.from_bytes(raw[1:3], "big") == self.s.locate):
                current = time.monotonic_ns()
                quote = self.core.quote(current)
                self.ai_gate.observe(sequence=first_sequence + offset,
                                     source_timestamp_ns=int.from_bytes(raw[5:11], "big"),
                                     market=market_from_quote(quote), now_ns=current,
                                     inventory=self.core.snapshot().position, market_fresh=bool(quote.market_fresh),
                                     status_event=raw[0:1] in (b"S", b"H", b"h"))

    def ai_participation(self, now, quote=None):
        if not self.ai_gate:
            return {"allow_bid": True, "allow_ask": True}
        quote = quote or self.core.quote(now)
        result = self.ai_gate.current(now_ns=now, inventory=self.core.snapshot().position,
                                      market=market_from_quote(quote), market_fresh=bool(quote.market_fresh))
        # Registry verification uses disk in this external adapter. Its elapsed
        # time cannot turn an expired signal into permission at the send boundary.
        if time.monotonic_ns() >= result["expires_ns"]:
            result.update(allow_bid=False, allow_ask=False, action="WAIT", reason_codes=["TTL_EXPIRED"])
        self.ai_gate.last_signal = dict(result)
        for order in self.orders.values():
            if not order.terminal and not result["allow_bid" if order.side == 0 else "allow_ask"]:
                order.cancel_requested = True
        return result

    def can_price(self, price):
        applicable = [increment for edge, increment in self.market_rules if price >= edge]
        return bool(applicable and price % applicable[-1] == 0)

    def send_order(self, side, quantity, price, now):
        if self.killed or not self.armed or not self.can_price(price):
            return
        if (not self.broker.client.isConnected() or not self.broker.events.empty()
                or self.broker.overflow.is_set() or self.broker_ledger_pending()):
            return
        if not self.order_account_gate(side, quantity, price, now):
            return
        ai_signal = self.ai_participation(now)
        if not ai_signal["allow_bid" if side == 0 else "allow_ask"]:
            return
        # Leave one shared credit available to cancellation traffic.
        if not self.new_bucket.available() or not self.wire_bucket.available(2):
            return
        if len(self.orders) >= 100_000 or self.next_id >= 2_147_483_647:
            raise SafetyFault("Paper order identity/session capacity exhausted.")
        sdk_order = self.broker.build_order(side, quantity, price)
        broker_id = self.next_id
        while broker_id == self.resources.last_request_id or broker_id in {9002, 9003}:
            broker_id += 1
        self.next_id = broker_id
        self.next_id += 1
        internal = self.journal.allocate_order(broker_id, side, quantity, price)
        if self.ai_gate:
            self.journal.audit("ai_send_candidate", {"order_id": internal, "signal": ai_signal})
        permit = self.core.reserve(internal, side, quantity, price, now, now + 100_000_000)
        if permit is None:
            self.journal.order_state(internal, "risk_rejected")
            return
        order = WorkingOrder(internal, broker_id, side, quantity, price, now)
        self.orders[broker_id] = order
        # AI approval validation may perform local disk reads. It must finish
        # before the final C++ permit/deadline/freshness boundary, never after it.
        ai_signal = self.ai_participation(time.monotonic_ns())
        if (not self.broker.client.isConnected() or not self.broker.events.empty() or self.broker.overflow.is_set()
                or self.killed or self.broker_ledger_pending()
                or not self.order_account_gate(side, quantity, price, time.monotonic_ns(), already_reserved=True)
                or not ai_signal["allow_bid" if side == 0 else "allow_ask"]):
            self.core.call("definitely_not_sent", internal)
            order.terminal = True
            self.journal.order_state(internal, "not_sent")
            return
        try:
            self.core.send_boundary(permit, time.monotonic_ns())
        except CoreError:
            self.core.call("definitely_not_sent", internal)
            order.terminal = True
            self.journal.order_state(internal, "not_sent")
            return
        boundary_now = time.monotonic_ns()
        if (not self.broker.client.isConnected() or not self.broker.events.empty() or self.broker.overflow.is_set()
                or self.killed or self.broker_ledger_pending() or boundary_now >= permit.deadline_ns
                or not self.resources.fresh(boundary_now)
                or (self.ai_gate and boundary_now >= ai_signal["expires_ns"])):
            self.core.call("definitely_not_sent", internal)
            order.terminal = True
            self.journal.order_state(internal, "not_sent")
            return
        self.new_bucket.take()
        self.wire_bucket.take()
        # Once the SDK call starts, any exception is conservatively unknown.
        try:
            self.broker.client.placeOrder(broker_id, self.broker.contract, sdk_order)
        except Exception:
            self.unknown(order, "SDK send outcome is unknown; automatic resend prohibited.")
            return
        self.journal.order_state(internal, "submitted_locally")
        log("paper_order_submitted_locally", internal_order_id=internal, side=side, quantity=quantity,
            price_units=price, exchange_ack=False)

    def drive_cancels(self, now):
        for order in self.orders.values():
            if order.terminal or not order.cancel_requested or order.cancel_sent_ns:
                continue
            if not self.broker.client.isConnected() or not self.cancel_bucket.available() or not self.wire_bucket.available():
                return
            try:
                self.core.call("cancel_request", order.internal_id, accepted=(0, 2))
            except Exception:
                self.fault("Core cancel-state update failed; broker cancellation still required.")
            self.cancel_bucket.take()
            self.wire_bucket.take()
            order.cancel_sent_ns = now
            try:
                self.broker.cancel(order.broker_id)
            except Exception:
                order.unknown = True
                self.fault("Cancellation outcome is unknown.", 4)
                try:
                    self.core.call("outcome_unknown", order.internal_id, accepted=(0, 5))
                except Exception:
                    pass
            try:
                self.journal.audit("cancel_requested", {"order_id": order.internal_id})
            except Exception:
                self.fault("Cancel audit persistence failed; account requires reconciliation.")

    def drive_control_requests(self):
        if self.killed or not self.broker.client.isConnected():
            return
        while self.broker.requests and self.wire_bucket.available():
            name, args = self.broker.requests.popleft()
            self.wire_bucket.take()
            getattr(self.broker.client, name)(*args)

    def write_status(self, now, state=None):
        if not self.status_file or now - self.last_status_file < 1_000_000_000:
            return
        self.last_status_file = now
        snapshot = self.core.snapshot()
        connected = self.broker.client.isConnected()
        status = {
            "schema_version": 1, "kind": "ibkr-paper", "updated_at_ns": time.time_ns(),
            "state": state or ("disconnected" if not connected else "reconcile_required" if self.killed
                               else "halted" if self.armed and (self.funds_gate_reason or
                                   (self.ai_gate and self.ai_gate.last_signal["action"] == "WAIT"))
                               else "running" if self.armed else "awaiting_feed"),
            "connected": connected, "armed": bool(snapshot.armed and not self.killed),
            "inventory": snapshot.position, "open_orders": snapshot.open_orders,
            "net_pnl_units": snapshot.net_pnl_units if snapshot.pnl_mark_valid else None,
            "pnl_mark_valid": bool(snapshot.pnl_mark_valid),
            "last_error_code": "PAPER_SAFETY_LATCH" if self.killed else "",
            "kill_latched": bool(self.killed or snapshot.killed),
            "funds_ready": self.resources.fresh(now) and not self.funds_gate_reason,
            "account_gate_reason": self.funds_gate_reason,
            "ai_enabled": self.ai_gate is not None,
            "ai_signal": self.ai_gate.last_signal if self.ai_gate else None,
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".paper-status-", dir=self.status_file.parent)
        try:
            with os.fdopen(descriptor, "w") as target:
                json.dump(status, target, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.status_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def tick(self, now):
        for bucket in (self.new_bucket, self.cancel_bucket, self.wire_bucket):
            bucket.refill(now)
        if self.broker.overflow.is_set():
            self.fault("SDK callback queue or reader failed.")
        if self.armed and not self.broker.client.isConnected():
            self.fault("Broker socket disconnected.", 3)
        if not self.killed:
            if (self.resources.pending_request is not None
                    and now - self.resources.pending_since_ns > self.s.funds_max_age_ms * 1_000_000):
                request_id = self.resources.pending_request
                self.resources.abandon(request_id)
                self.broker.request("cancelAccountSummary", request_id)
                self.pause_increasing_quotes("FUNDS_QUERY_TIMEOUT")
            if (self.next_id is not None and self.resources.pending_request is None
                    and now - self.last_funds_request >= self.s.funds_refresh_ms * 1_000_000):
                self.begin_funds_query(now)
            if self.last_broker_time and now - self.last_broker_time > 30_000_000_000:
                self.fault("Broker heartbeat expired.", 3)
            if now - self.last_time_request > 10_000_000_000 and self.next_id is not None:
                self.broker.request("reqCurrentTime")
                self.last_time_request = now
            if self.reconcile_started and not self.reconcile_at and now - self.reconcile_started > self.s.broker_timeout * 1e9:
                self.fault("Broker reconciliation did not complete.", 3)
        if not self.armed and self.next_id is None and now - self.started > self.s.broker_timeout * 1e9:
            self.fault("Broker handshake timed out.", 3)
        for order in self.orders.values():
            if (order.unmatched_fill_since_ns and not order.unknown
                    and now - order.unmatched_fill_since_ns > self.s.ack_timeout_ms * 1_000_000):
                self.unknown(order, "Reported cumulative fill has no matching execution; risk retained.")
            if order.terminal:
                continue
            if not order.acknowledged and not order.unknown and now - order.created_ns > self.s.ack_timeout_ms * 1_000_000:
                self.unknown(order, "Order ACK timed out; reservation retained.")
            if order.cancel_sent_ns and not order.unknown and now - order.cancel_sent_ns > self.s.ack_timeout_ms * 1_000_000:
                self.unknown(order, "Cancel ACK timed out; reservation retained.")
            if now - order.created_ns > self.s.quote_lifetime_ms * 1_000_000:
                order.cancel_requested = True
        if not self.killed:
            account_ready = self.account_gate_ready(now)
            connected = self.broker.client.isConnected() and self.accounts_verified and bool(self.last_broker_time)
            feed_ready = self.feed.started and self.directory_verified
            self.core.health(broker=connected, system=True, feed=feed_ready, sequence=feed_ready, live=feed_ready)
            quote = self.core.quote(now)
            ai_signal = self.ai_participation(now, quote)
            if self.armed and (not quote.market_fresh or not quote.trading_status_valid):
                self.fault("Market stale or trading status invalid.", 2)
            elif (quote.quote_available and self.market_rules and self.reconcile_at and not self.armed
                  and account_ready and self.broker.events.empty()
                  and not self.broker.overflow.is_set() and not self.broker_ledger_pending()):
                if now - self.reconcile_at > 5_000_000_000:
                    self.begin_reconciliation(now)
                else:
                    self.mark(now)
                    self.journal.start_once(self.s.session_date)
                    self.core.call("arm_flat", 1, 1, 0, 0, now)
                    self.armed = True
                    self.journal.audit("paper_armed", {
                        "symbol": self.s.symbol, "con_id": self.s.con_id,
                        "quote_economics_units_1e4_usd": dict(zip(
                            ("fee_per_share", "minimum_fee", "slippage_buffer", "minimum_net_edge"),
                            self.s.quote_economics(), strict=True,
                        )),
                        "ai_model": {"version": self.ai_gate.version,
                                     "artifact_file_sha256": self.ai_gate.artifact_sha256,
                                     "policy_sha256": self.ai_gate.config.sha256} if self.ai_gate else None,
                    })
                    log("paper_armed", symbol=self.s.symbol, live=False)
            elif self.armed:
                self.mark(now)
            if (self.armed and not self.killed and self.broker.events.empty()
                    and account_ready and not self.broker_ledger_pending()
                    and now - self.last_quote >= self.s.quote_interval_ms * 1_000_000):
                self.last_quote = now
                for side, price, quantity in ((0, quote.bid_price, quote.bid_quantity), (1, quote.ask_price, quote.ask_quantity)):
                    permitted = ai_signal["allow_bid" if side == 0 else "allow_ask"]
                    working = [order for order in self.orders.values() if order.side == side and not order.terminal]
                    for order in working:
                        if not permitted or not quantity or order.price != price or order.quantity - order.filled != quantity:
                            order.cancel_requested = True
                    if not working and quantity and permitted:
                        self.send_order(side, min(quantity, self.s.order_size), price, time.monotonic_ns())
        for con_id, (quantity, first_mismatch) in list(self.pending_positions.items()):
            expected = self.pnl.position if con_id == self.s.con_id else 0
            if quantity != expected:
                if not first_mismatch:
                    self.pending_positions[con_id] = (quantity, now)
                elif now - first_mismatch >= 2_000_000_000:
                    self.fault("Broker position disagrees with local execution ledger.", 4)
            elif first_mismatch:
                self.pending_positions[con_id] = (quantity, 0)
        self.drive_cancels(now)
        self.drive_control_requests()
        try:
            self.write_status(now)
        except Exception:
            self.fault("Operator status publication failed.")
        if now - self.last_status >= 5_000_000_000:
            snapshot = self.core.snapshot()
            log("paper_status", armed=bool(snapshot.armed), killed=bool(snapshot.killed),
                position=snapshot.position, open_orders=snapshot.open_orders,
                pnl_units=snapshot.net_pnl_units, pnl_mark_valid=bool(snapshot.pnl_mark_valid),
                sequence=self.feed.expected - 1, mode="paper")
            self.last_status = now

    def run(self):
        receiver = open_multicast(self.s.group, self.s.port, self.s.interface, self.s.source_ip)
        stopping = threading.Event()
        previous = {sig: signal.signal(sig, lambda *_: stopping.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            self.broker.start()
            log("paper_waiting_for_verified_full_session", symbol=self.s.symbol, sequence_required=1, live=False)
            while True:
                now = time.monotonic_ns()
                if stopping.is_set() and not self.shutdown_deadline:
                    self.fault("Operator requested shutdown.", 1)
                    self.shutdown_deadline = now + 30_000_000_000
                try:
                    for _ in range(128):
                        try:
                            kind, data = self.broker.events.get_nowait()
                        except queue.Empty:
                            break
                        self.on_event(kind, data, time.monotonic_ns())
                    if not self.killed:
                        for _ in range(64):
                            try:
                                packet, address = receiver.recvfrom(65535)
                            except BlockingIOError:
                                break
                            self.process_packet(packet, address[0], time.monotonic_ns())
                    self.tick(time.monotonic_ns())
                except Exception as exc:
                    self.fault(str(exc))
                if self.killed and not self.shutdown_deadline:
                    self.shutdown_deadline = time.monotonic_ns() + 30_000_000_000
                if self.shutdown_deadline and time.monotonic_ns() >= self.shutdown_deadline:
                    break
                # External SDK/control-loop wait, not a C++ hot-path sleep.
                try:
                    select.select([] if self.killed else [receiver], [], [], 0.01)
                except OSError:
                    self.fault("Market receiver wait failed.", 2)
        finally:
            receiver.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            try:
                self.broker.close()
            except Exception:
                self.fault("Broker reader shutdown failed; cancellation unconfirmed.")
            snapshot = self.core.snapshot()
            confirmed = not any(not order.terminal or order.unknown or order.reported_filled > order.filled
                                for order in self.orders.values())
            try:
                self.journal.finish(self.reason or "stopped")
            except Exception:
                self.fault("Final journal persistence failed.")
            try:
                self.last_status_file = 0
                self.write_status(time.monotonic_ns(), state="reconcile_required")
            except Exception:
                pass
            log("paper_stopped", cancellation_confirmed=confirmed, position=snapshot.position,
                flat=snapshot.position == 0, reconciliation_required=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-paper", action="store_true", help="Explicitly permit paper orders after all safety gates.")
    parser.add_argument("--status-file", type=Path, help="Optional private atomic status file for a read-only operator view.")
    args = parser.parse_args(argv)
    core = broker = journal = None
    try:
        settings = Settings.from_env()
        settings.validate(args.enable_paper)
        core = Core(settings.library, settings.core_config())
        # Must be set while disarmed, before any feed, broker connection, arm,
        # reservation or send. Older libraries lacking this export fail closed.
        core.set_quote_economics(*settings.quote_economics())
        ai_gate = (AIParticipationGate(settings.ai_registry, settings.symbol, settings.ai_policy_config())
                   if settings.ai_registry else None)
        broker = Broker(settings)
        if not args.enable_paper:
            log("paper_preflight_completed", network_connected=False, trading_enabled=False,
                message="C ABI and SDK capabilities loaded; no broker/feed validation performed.")
            return 0
        journal = Journal(settings.state_dir, settings.account, settings.client_id)
        runner = PaperRunner(settings, core, broker, journal, status_file=args.status_file, ai_gate=ai_gate)
        runner.run()
        return 2 if runner.killed else 0
    except (SafetyFault, AIGateError, CoreError, FeedFault, LedgerFault, ProbeError, OSError, ValueError, ImportError) as exc:
        log("paper_unavailable", message=str(exc), trading_enabled=False)
        return 2
    finally:
        if journal:
            journal.close()
        if core:
            core.close()


if __name__ == "__main__":
    raise SystemExit(main())
