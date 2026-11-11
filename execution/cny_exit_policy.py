"""Independent long-only CNY holding-value exit policy, not a broker sender.

The future execution owner must persist the returned latch before actions,
block its buy path, and reconcile/reserve each sell through ordinary risk/OMS.
An idempotent zero-position target is NOT permission to repeatedly send sells.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum


class ExitFault(ValueError):
    pass


class Phase(str, Enum):
    MONITOR = "MONITOR"
    WAIT = "WAIT"
    CANCEL_BUYS = "CANCEL_BUYS"
    EXIT_WORKING = "EXIT_WORKING"
    FLAT_LOCKED = "FLAT_LOCKED"


@dataclass(frozen=True)
class ExitRule:
    threshold_cny: Decimal = Decimal("5000000")
    stock_max_age_ns: int = 2_000_000_000
    fx_max_age_ns: int = 5_000_000_000
    account_max_age_ns: int = 2_000_000_000

    def __post_init__(self):
        money(self.threshold_cny)
        for age in (self.stock_max_age_ns, self.fx_max_age_ns, self.account_max_age_ns):
            if type(age) is not int or not 0 < age <= 60_000_000_000:
                raise ExitFault("Invalid independent data age bound")


@dataclass(frozen=True)
class Mark:
    value: Decimal
    # Conservative source-time freshness, converted to this process's monotonic
    # clock by the source adapter. Never reset this to 'now' at consumption.
    asof_monotonic_ns: int
    base: str
    quote: str


@dataclass(frozen=True)
class OpenOrder:
    identity: str
    side: str
    leaves: int
    state: str = "WORKING"


@dataclass(frozen=True)
class Holding:
    account: str
    con_id: int
    quantity: int
    orders: tuple[OpenOrder, ...]
    asof_monotonic_ns: int
    reconciled: bool
    broker_healthy: bool
    tradable: bool


@dataclass(frozen=True)
class ExitState:
    account: str
    con_id: int
    triggered: bool = False


@dataclass(frozen=True)
class ExitPlan:
    state: ExitState
    phase: Phase
    block_new_buys: bool
    target_position: int | None
    cancel_buy_ids: tuple[str, ...] = ()
    cancel_sell_ids: tuple[str, ...] = ()
    max_additional_sell_quantity: int = 0
    market_value_cny: Decimal | None = None
    reason: str = ""
    # Even EXIT_WORKING is a target for risk/OMS, never a broker order permit.
    execution_authorized: bool = False


def money(value):
    if (not isinstance(value, Decimal) or not value.is_finite() or not 0 < value <= Decimal("1e12")
            or len(value.as_tuple().digits) > 24 or value.as_tuple().exponent < -12):
        raise ExitFault("Positive, bounded, exact Decimal value required")


def fresh(asof, now, limit):
    return type(asof) is int and 0 <= asof <= now and now - asof <= limit


def mark_valid(mark, base, quote, now, limit):
    if mark is None or mark.base != base or mark.quote != quote:
        return False
    try:
        money(mark.value)
    except ExitFault:
        return False
    return fresh(mark.asof_monotonic_ns, now, limit)


def evaluate(rule: ExitRule, state: ExitState, holding: Holding, *, stock_bid: Mark | None,
             usd_cny: Mark | None, now_ns: int) -> ExitPlan:
    """Single account/contract; complete local+broker reconciliation is required.

    Holding.orders includes every nonterminal order and every reserved or
    unknown send for this account/contract, across strategy/client ownership.
    The owner must reject snapshots it cannot safely cancel or reconcile.
    """
    if (not state.account or type(state.con_id) is not int or state.con_id <= 0
            or (state.account, state.con_id) != (holding.account, holding.con_id)):
        raise ExitFault("Exit state and holding identities must match")
    if type(state.triggered) is not bool or type(now_ns) is not int or now_ns < 0:
        raise ExitFault("Invalid latch or monotonic time")
    if type(holding.quantity) is not int or not 0 <= holding.quantity <= 10**9:
        raise ExitFault("This exit policy supports nonnegative integer long positions only")
    if any(type(v) is not bool for v in (holding.reconciled, holding.broker_healthy, holding.tradable)):
        raise ExitFault("Health/reconciliation flags must be explicit booleans")
    if not isinstance(holding.orders, tuple) or len(holding.orders) > 10000:
        raise ExitFault("Bounded immutable order snapshot required")
    identities = set()
    for order in holding.orders:
        if (not isinstance(order.identity, str) or not 1 <= len(order.identity) <= 256
                or order.identity in identities or order.side not in {"BUY", "SELL"}
                or order.state not in {"WORKING", "CANCEL_PENDING", "SEND_UNKNOWN", "RESERVED"}
                or type(order.leaves) is not int or not 0 <= order.leaves <= 10**9):
            raise ExitFault("Invalid or duplicate nonterminal order")
        identities.add(order.identity)

    buys = tuple(o.identity for o in holding.orders if o.side == "BUY")
    sells = tuple(o.identity for o in holding.orders if o.side == "SELL")
    reserved_sells = sum(o.leaves for o in holding.orders if o.side == "SELL")
    value = None

    def plan(phase, reason, *, sell=0, cancel_sells=None, block=True):
        # Removing buy risk must not hide existing excess sell risk. Keep both
        # cancellation intents even while waiting for buy terminals/health.
        if cancel_sells is None:
            cancel_sells = sells if state.triggered and (
                reserved_sells > holding.quantity or (holding.quantity == 0 and sells)) else ()
        return ExitPlan(state, phase, block, 0 if state.triggered else None,
                        buys if state.triggered else (), cancel_sells, sell, value, reason)

    if not holding.reconciled or not fresh(holding.asof_monotonic_ns, now_ns, rule.account_max_age_ns):
        return plan(Phase.WAIT, "ACCOUNT_NOT_RECONCILED_OR_STALE")
    if not state.triggered:
        if not mark_valid(stock_bid, str(holding.con_id), "USD", now_ns, rule.stock_max_age_ns):
            return plan(Phase.WAIT, "STOCK_MARK_UNAVAILABLE")
        if not mark_valid(usd_cny, "USD", "CNY", now_ns, rule.fx_max_age_ns):
            return plan(Phase.WAIT, "USD_CNY_MARK_UNAVAILABLE")
        # Independent precision avoids caller decimal context changing threshold.
        with localcontext() as context:
            context.prec = 80
            value = Decimal(holding.quantity) * stock_bid.value * usd_cny.value
        if value < rule.threshold_cny:
            if not holding.broker_healthy or not holding.tradable or any(o.state == "SEND_UNKNOWN" for o in holding.orders):
                return plan(Phase.WAIT, "ACCOUNT_OR_MARKET_NOT_READY")
            return plan(Phase.MONITOR, "BELOW_THRESHOLD", block=False)
        state = ExitState(state.account, state.con_id, triggered=True)

    # From this point the zero target never depends on AI, falling value, or FX.
    # Buy cancellation remains requested even with bad prices or broker health.
    if buys:
        return plan(Phase.CANCEL_BUYS, "WAIT_FOR_BUY_TERMINALS_AND_RECONCILIATION")
    if not holding.broker_healthy:
        return plan(Phase.WAIT, "BROKER_UNHEALTHY")
    if any(o.state == "SEND_UNKNOWN" for o in holding.orders):
        return plan(Phase.WAIT, "UNKNOWN_SEND_REQUIRES_RECONCILIATION")
    if reserved_sells > holding.quantity or (holding.quantity == 0 and sells):
        return plan(Phase.WAIT, "REMOVE_EXCESS_OR_RESIDUAL_SELL_ORDERS", cancel_sells=sells)
    if holding.quantity == 0:
        return plan(Phase.FLAT_LOCKED, "FLAT_CONFIRMED_REENTRY_REQUIRES_SUPERVISOR")
    if not holding.tradable:
        return plan(Phase.WAIT, "HALTED_OR_OUTSIDE_AUTHORIZED_SESSION")
    if not mark_valid(stock_bid, str(holding.con_id), "USD", now_ns, rule.stock_max_age_ns):
        return plan(Phase.WAIT, "STOCK_MARK_UNAVAILABLE")
    return plan(Phase.EXIT_WORKING, "REDUCE_TO_ZERO_THROUGH_RISK_AND_OMS",
                sell=holding.quantity - reserved_sells)
