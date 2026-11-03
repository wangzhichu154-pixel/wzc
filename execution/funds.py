"""Owner-thread account resource guard, independent of model and C++ risk.

Only complete, exact-account USD broker summaries become usable. The session
budget never credits sales or forgets locally observed purchases on a refresh.
BuyingPower is an extra ceiling, not cash or a grant of trading permission.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR


class FundsError(RuntimeError):
    pass


REQUIRED_TAGS = frozenset({"AvailableFunds", "SettledCash", "BuyingPower"})


def money_units(raw: str) -> int:
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise FundsError("Broker account resource value is not a valid decimal.") from exc
    if not value.is_finite() or abs(value) >= Decimal(2**62) / 10_000:
        raise FundsError("Broker account resource value is non-finite or out of range.")
    # Floor availability rather than rounding a fractional credit upwards.
    return int((value * 10_000).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class FundsSnapshot:
    request_id: int
    received_ns: int
    available_units: int
    settled_cash_units: int
    buying_power_units: int

    @property
    def ceiling(self) -> int:
        return max(0, min(self.available_units, self.settled_cash_units, self.buying_power_units))


class AccountResources:
    def __init__(self, account: str, max_age_ns: int):
        if not account or type(max_age_ns) is not int or max_age_ns <= 0:
            raise FundsError("Account resource guard requires account identity and freshness limit.")
        self.account = account
        self.max_age_ns = max_age_ns
        self.snapshot: FundsSnapshot | None = None
        self.initial_ceiling: int | None = None
        self.conservative_ceiling: int | None = None
        self.pending_request: int | None = None
        self.pending_since_ns = 0
        self.pending_values: dict[str, int] = {}
        self.pending_invalid = False
        self.last_request_id = -1
        self.invalid_reason = "FUNDS_MISSING"
        self.gross_buy_spend = 0
        self.fee_high_watermark = 0

    def begin(self, request_id: int, now_ns: int) -> None:
        if request_id <= self.last_request_id or self.pending_request is not None or now_ns < 0:
            raise FundsError("Account summary requests must be non-overlapping and monotonic.")
        self.last_request_id = request_id
        self.pending_request = request_id
        self.pending_since_ns = now_ns
        self.pending_values.clear()
        self.pending_invalid = False

    def invalidate(self, reason="FUNDS_INVALID") -> None:
        self.invalid_reason = reason
        if self.pending_request is not None:
            self.pending_invalid = True

    def receive(self, request_id: int, account: str, tag: str, value: str, currency: str) -> bool:
        if request_id != self.pending_request or account != self.account or tag not in REQUIRED_TAGS:
            return False
        if currency != "USD":
            self.invalidate("FUNDS_CURRENCY_UNVERIFIED")
            raise FundsError("Exact-account USD resources are required; BASE is not assumed to be USD.")
        try:
            units = money_units(value)
        except FundsError:
            self.invalidate()
            raise
        if tag in self.pending_values:
            # Multiple callbacks in one snapshot may reflect an update; retain
            # the less optimistic value, never upgrade a batch from duplicates.
            units = min(units, self.pending_values[tag])
        self.pending_values[tag] = units
        return True

    def complete(self, request_id: int, now_ns: int) -> bool:
        if request_id != self.pending_request:
            return False
        values = dict(self.pending_values)
        since = self.pending_since_ns
        invalid = self.pending_invalid
        self.pending_request = None
        self.pending_values.clear()
        if invalid or set(values) != REQUIRED_TAGS or now_ns < since or now_ns - since > self.max_age_ns:
            self.invalidate("FUNDS_INCOMPLETE")
            return False
        snapshot = FundsSnapshot(request_id, since, values["AvailableFunds"], values["SettledCash"], values["BuyingPower"])
        self.snapshot = snapshot
        if self.initial_ceiling is None:
            self.initial_ceiling = snapshot.ceiling
        self.conservative_ceiling = min(
            snapshot.ceiling, self.conservative_ceiling if self.conservative_ceiling is not None else snapshot.ceiling
        )
        self.invalid_reason = "" if snapshot.ceiling > 0 else "FUNDS_NONPOSITIVE"
        return not self.invalid_reason

    def abandon(self, request_id: int, reason="FUNDS_QUERY_TIMEOUT") -> None:
        if request_id == self.pending_request:
            self.pending_request = None
            self.pending_values.clear()
            self.invalidate(reason)

    def fresh(self, now_ns: int) -> bool:
        return bool(self.snapshot and not self.invalid_reason
                    and self.snapshot.received_ns <= now_ns
                    and now_ns - self.snapshot.received_ns <= self.max_age_ns)

    def remaining(self, now_ns: int, reserved_buy_units: int) -> int | None:
        if type(reserved_buy_units) is not int or reserved_buy_units < 0:
            raise FundsError("Invalid local resource reservation.")
        if not self.fresh(now_ns) or self.initial_ceiling is None:
            return None
        ceiling = min(self.initial_ceiling, self.snapshot.ceiling, self.conservative_ceiling)
        return ceiling - self.gross_buy_spend - self.fee_high_watermark - reserved_buy_units

    def can_reserve_buy(self, now_ns: int, reserved_buy_units: int, additional_units: int) -> bool:
        if type(additional_units) is not int or additional_units < 0:
            raise FundsError("Invalid prospective resource reservation.")
        available = self.remaining(now_ns, reserved_buy_units)
        return available is not None and available >= additional_units

    def record_buy_fill(self, quantity: int, price_units: int) -> None:
        if type(quantity) is not int or type(price_units) is not int or quantity <= 0 or price_units <= 0:
            raise FundsError("Invalid cash debit from execution.")
        self.gross_buy_spend += quantity * price_units
        if self.gross_buy_spend >= 2**62:
            self.invalidate("FUNDS_OVERFLOW")
            raise FundsError("Session purchase accounting exceeded its supported bound.")

    def observe_positive_fees(self, total_units: int) -> None:
        if type(total_units) is not int or not 0 <= total_units < 2**62:
            self.invalidate("FUNDS_INVALID_FEE")
            raise FundsError("Invalid cumulative positive fee debit.")
        self.fee_high_watermark = max(self.fee_high_watermark, total_units)


@dataclass(frozen=True)
class TradingPermissions:
    """Operator-verified cash-only stock scope, not derived from ML or balances."""
    account: str
    stock_permission_attested: bool

    def may_buy(self, account: str) -> bool:
        return account == self.account and self.stock_permission_attested

    def may_sell(self, account: str, *, position: int, reserved_sells: int, quantity: int) -> bool:
        # No implementation verifies borrowing/locate/short-sale permissions.
        # Therefore this version supports reductions of existing long inventory only.
        return (account == self.account and self.stock_permission_attested and quantity > 0
                and reserved_sells >= 0 and position - reserved_sells >= quantity)
