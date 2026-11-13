"""Direct HTTPS Alpaca adapter, restricted to the Paper Trading endpoint.

This adapter is transport and defensive order validation, not a complete risk
engine. The single execution owner must journal stable client IDs, reconcile
unknown sends and enforce strategy, portfolio, quote and loss limits before use.
No endpoint override, implicit retry, historical-to-live permission, or live send.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request


TRADING_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2"
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,15}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")


class AlpacaError(RuntimeError):
    """Safe fixed error information; response bodies and keys are never retained."""

    def __init__(self, code, *, http_status=None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


class UnknownMutation(AlpacaError):
    def __init__(self, client_order_id):
        self.client_order_id = client_order_id
        super().__init__("MUTATION_RESULT_UNKNOWN_RECONCILE_BEFORE_RETRY")


@dataclass(frozen=True)
class AlpacaConfig:
    key_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    feed: str = "iex"
    enable_paper: bool = False
    expected_account_id: str = ""
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("ALPACA_PAPER_KEY_ID", ""),
                   os.environ.get("ALPACA_PAPER_SECRET_KEY", ""),
                   feed=os.environ.get("ALPACA_DATA_FEED", "iex"),
                   enable_paper=os.environ.get("ALPACA_ENABLE_PAPER") == "true",
                   expected_account_id=os.environ.get("ALPACA_EXPECTED_ACCOUNT_ID", ""))

    def validate(self):
        for value in (self.key_id, self.secret_key):
            if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(not 33 <= ord(c) <= 126 for c in value):
                raise AlpacaError("MISSING_OR_INVALID_PAPER_CREDENTIALS")
        if self.feed not in {"iex", "sip"}:
            raise AlpacaError("REALTIME_FEED_MUST_BE_IEX_OR_SIP")
        if type(self.enable_paper) is not bool or not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 30:
            raise AlpacaError("INVALID_ADAPTER_CONFIGURATION")
        if self.expected_account_id and not IDENTIFIER.fullmatch(self.expected_account_id):
            raise AlpacaError("INVALID_EXPECTED_ACCOUNT_ID")


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal
    client_order_id: str
    time_in_force: str = "ioc"
    expires_at: datetime | None = None

    def validate(self):
        if not isinstance(self.symbol, str) or not SYMBOL.fullmatch(self.symbol) or self.side not in {"buy", "sell"}:
            raise AlpacaError("INVALID_ORDER_INSTRUMENT_OR_SIDE")
        if type(self.quantity) is not int or not 1 <= self.quantity <= 1_000_000:
            raise AlpacaError("POSITIVE_INTEGER_SHARES_REQUIRED")
        price = money(self.limit_price)
        tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
        if not isinstance(self.limit_price, Decimal) or price % tick != 0:
            raise AlpacaError("EXACT_LIMIT_PRICE_ON_TICK_REQUIRED")
        if not isinstance(self.client_order_id, str) or not IDENTIFIER.fullmatch(self.client_order_id):
            raise AlpacaError("STABLE_CLIENT_ORDER_ID_REQUIRED")
        if self.time_in_force not in {"day", "ioc"}:
            raise AlpacaError("UNSUPPORTED_TIME_IN_FORCE")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise AlpacaError("AWARE_SEND_DEADLINE_REQUIRED")
        remaining = (self.expires_at - datetime.now(self.expires_at.tzinfo)).total_seconds()
        if not 0 < remaining <= 30:
            raise AlpacaError("ORDER_DEADLINE_EXPIRED_OR_TOO_LONG")


def money(value, *, zero=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise AlpacaError("INVALID_BROKER_NUMERIC_FIELD") from None
    if (not result.is_finite() or result < 0 or (not zero and result == 0) or result > Decimal("1e15")
            or len(result.as_tuple().digits) > 24 or result.as_tuple().exponent < -12):
        raise AlpacaError("INVALID_BROKER_NUMERIC_FIELD")
    return result


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise AlpacaError("INVALID_ORDER_ID")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AlpacaError("REDIRECT_REFUSED")


class AlpacaClient:
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def __init__(self, config: AlpacaConfig):
        config.validate()
        self.config = config
        self._opener = urllib.request.build_opener(NoRedirect())

    def _request(self, method, path, *, data_api=False, params=None, body=None, mutation_identity=None):
        # Paths originate exclusively in methods below, not model/API input.
        if not path.startswith("/") or "?" in path or "#" in path or ".." in path:
            raise AlpacaError("INVALID_INTERNAL_PATH")
        base = DATA_URL if data_api else TRADING_URL
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"APCA-API-KEY-ID": self.config.key_id, "APCA-API-SECRET-KEY": self.config.secret_key,
                   "Accept": "application/json", "User-Agent": "Vector-Paper/1"}
        payload = None
        if body is not None:
            payload = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        mutating = method != "GET"
        try:
            with self._opener.open(request, timeout=self.config.timeout_seconds) as response:
                if response.status not in {200, 201, 204}:
                    raise AlpacaError("UNEXPECTED_RESPONSE_STATUS", http_status=response.status)
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
                if len(raw) > self.MAX_RESPONSE_BYTES:
                    raise AlpacaError("RESPONSE_TOO_LARGE")
                if response.status == 204:
                    return None
                return json.loads(raw, parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            # 5xx can occur after accepting a mutation; never infer failure.
            if mutating and (status >= 500 or status == 408):
                raise UnknownMutation(mutation_identity) from None
            raise AlpacaError("BROKER_HTTP_ERROR", http_status=status) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError, AlpacaError):
            if mutating:
                raise UnknownMutation(mutation_identity) from None
            raise AlpacaError("TRANSPORT_OR_RESPONSE_INVALID") from None

    def _object(self, path, **kwargs):
        result = self._request("GET", path, **kwargs)
        if not isinstance(result, dict):
            raise AlpacaError("EXPECTED_OBJECT_RESPONSE")
        return result

    def get_account(self):
        return self._object("/account")

    def get_clock(self):
        return self._object("/clock")

    def get_asset(self, symbol):
        self._symbols([symbol])
        return self._object("/assets/" + symbol)

    def get_positions(self):
        result = self._request("GET", "/positions")
        if not isinstance(result, list) or len(result) > 10000 or any(not isinstance(p, dict) for p in result):
            raise AlpacaError("INVALID_POSITIONS_RESPONSE")
        return result

    def get_orders(self):
        result = self._request("GET", "/orders", params={"status": "open", "limit": 500, "nested": "false"})
        if not isinstance(result, list) or len(result) >= 500 or any(not isinstance(o, dict) for o in result):
            raise AlpacaError("OPEN_ORDERS_INCOMPLETE_OR_INVALID")
        # Nested/complex orders are outside this independent owner's contract.
        if any(order.get("legs") or order.get("order_class", "") not in {"", "simple"} for order in result):
            raise AlpacaError("COMPLEX_ORDER_REQUIRES_MANUAL_RECONCILIATION")
        return result

    def get_order_by_client_id(self, client_order_id):
        try:
            return self._object("/orders:by_client_order_id", params={"client_order_id": identifier(client_order_id)})
        except AlpacaError as exc:
            if exc.http_status == 404:
                return None
            raise

    @staticmethod
    def _symbols(symbols):
        if (not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= 100
                or len(set(symbols)) != len(symbols)
                or any(not isinstance(s, str) or not SYMBOL.fullmatch(s) for s in symbols)):
            raise AlpacaError("EXPLICIT_BOUNDED_SYMBOL_LIST_REQUIRED")

    def latest_quotes(self, symbols):
        self._symbols(symbols)
        result = self._object("/stocks/quotes/latest", data_api=True,
                              params={"symbols": ",".join(symbols), "feed": self.config.feed})
        quotes = result.get("quotes")
        if not isinstance(quotes, dict) or set(quotes) != set(symbols) or any(not isinstance(q, dict) for q in quotes.values()):
            raise AlpacaError("INCOMPLETE_QUOTE_RESPONSE")
        # Caller checks quote source timestamps and bid/ask before every send.
        return quotes

    def daily_bars(self, symbols, start, end, *, adjustment="raw"):
        self._symbols(symbols)
        if adjustment not in {"raw", "all", "split", "dividend"}:
            raise AlpacaError("INVALID_PRICE_ADJUSTMENT")
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except (ValueError, TypeError):
            raise AlpacaError("DAILY_RANGE_REQUIRES_ISO_DATES") from None
        if first >= last or (last - first).days > 3653:
            raise AlpacaError("INVALID_DAILY_RANGE")
        # End is exclusive UTC midnight. The runner must choose a prior complete
        # trading day; this endpoint alone does not enforce completed sessions.
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start + "T00:00:00Z",
                  "end": end + "T00:00:00Z", "adjustment": adjustment, "feed": self.config.feed,
                  "limit": 10000, "sort": "asc"}
        output, tokens, total = {symbol: [] for symbol in symbols}, set(), 0
        for _ in range(100):
            page = self._object("/stocks/bars", data_api=True, params=params)
            rows = page.get("bars")
            if not isinstance(rows, dict) or not set(rows).issubset(symbols):
                raise AlpacaError("INVALID_BARS_RESPONSE")
            for symbol, bars in rows.items():
                if not isinstance(bars, list) or any(not isinstance(bar, dict) for bar in bars):
                    raise AlpacaError("INVALID_BARS_RESPONSE")
                output[symbol].extend(bars)
                total += len(bars)
            if total > 250000:
                raise AlpacaError("BAR_CAPACITY_EXCEEDED")
            token = page.get("next_page_token")
            if token is None:
                for bars in output.values():
                    times = [bar.get("t") for bar in bars]
                    if any(not isinstance(t, str) for t in times) or times != sorted(set(times)):
                        raise AlpacaError("BAR_TIMES_DUPLICATED_OR_UNORDERED")
                return output
            if not isinstance(token, str) or not 1 <= len(token) <= 2048 or token in tokens:
                raise AlpacaError("INVALID_OR_REPEATED_PAGINATION_TOKEN")
            tokens.add(token)
            params["page_token"] = token
        raise AlpacaError("BAR_PAGINATION_EXCEEDED")

    def _account_for_mutation(self, *, check_health=True):
        if not self.config.enable_paper or not self.config.expected_account_id:
            raise AlpacaError("EXPLICIT_PAPER_ENABLE_AND_ACCOUNT_ID_REQUIRED")
        account = self.get_account()
        if account.get("id") != self.config.expected_account_id:
            raise AlpacaError("ACCOUNT_ID_OR_TRADING_HEALTH_INVALID")
        if check_health and (account.get("status") != "ACTIVE" or account.get("currency") != "USD"
                or any(account.get(name) is not False for name in ("trading_blocked", "account_blocked", "trade_suspended_by_user"))):
            raise AlpacaError("ACCOUNT_ID_OR_TRADING_HEALTH_INVALID")
        return account

    def submit_order(self, order: OrderRequest):
        order.validate()
        account = self._account_for_mutation()
        asset = self.get_asset(order.symbol)
        if asset.get("symbol") != order.symbol or asset.get("class") != "us_equity" or asset.get("tradable") is not True or asset.get("status") != "active":
            raise AlpacaError("ASSET_NOT_TRADABLE_US_EQUITY")
        clock = self.get_clock()
        if clock.get("is_open") is not True:
            raise AlpacaError("REGULAR_MARKET_NOT_OPEN")
        try:
            timestamp = datetime.fromisoformat(clock["timestamp"].replace("Z", "+00:00"))
            now = datetime.now(timestamp.tzinfo)
            if timestamp.tzinfo is None or not -2 <= (now - timestamp).total_seconds() <= 30:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise AlpacaError("BROKER_CLOCK_STALE") from None
        orders = self.get_orders()
        if any(o.get("client_order_id") == order.client_order_id for o in orders) or self.get_order_by_client_id(order.client_order_id) is not None:
            raise AlpacaError("CLIENT_ORDER_ID_ALREADY_EXISTS")
        positions = self.get_positions()
        found = [p for p in positions if p.get("symbol") == order.symbol]
        if len(found) > 1:
            raise AlpacaError("DUPLICATE_POSITION_IDENTITY")
        position = money(found[0].get("qty"), zero=True) if found else Decimal(0)
        pending_sells, reserved_cash = Decimal(0), Decimal(0)
        for working in orders:
            leaves = money(working.get("qty"), zero=True) - money(working.get("filled_qty"), zero=True)
            if leaves < 0 or working.get("side") not in {"buy", "sell"}:
                raise AlpacaError("INVALID_OPEN_ORDER_ACCOUNTING")
            if working.get("side") == "sell" and working.get("symbol") == order.symbol:
                pending_sells += leaves
            elif working.get("side") == "buy":
                if working.get("type") != "limit":
                    raise AlpacaError("CANNOT_BOUND_PENDING_BUY_COST")
                reserved_cash += leaves * money(working.get("limit_price"))
        if order.side == "sell" and Decimal(order.quantity) > position - pending_sells:
            raise AlpacaError("SELL_EXCEEDS_UNRESERVED_LONG_POSITION")
        if order.side == "buy":
            available = min(money(account.get("cash"), zero=True), money(account.get("buying_power"), zero=True))
            if Decimal(order.quantity) * order.limit_price + reserved_cash > available:
                raise AlpacaError("INSUFFICIENT_UNRESERVED_CASH")
        body = {"symbol": order.symbol, "side": order.side, "qty": str(order.quantity),
                "type": "limit", "limit_price": format(order.limit_price, "f"), "time_in_force": order.time_in_force,
                "extended_hours": False, "client_order_id": order.client_order_id}
        # Prechecks above can take several network round trips. Revalidate the
        # source-derived deadline immediately at the final send boundary.
        order.validate()
        result = self._request("POST", "/orders", body=body, mutation_identity=order.client_order_id)
        if not isinstance(result, dict) or result.get("client_order_id") != order.client_order_id or not result.get("id"):
            raise UnknownMutation(order.client_order_id)
        return result

    def cancel_order(self, order_id):
        identifier(order_id)
        self._account_for_mutation(check_health=False)
        # Accepted cancellation is not terminal confirmation. Owner must continue
        # reserving leaves until order state and cumulative executions reconcile.
        self._request("DELETE", "/orders/" + order_id, mutation_identity=order_id)
        return {"cancel_requested": True, "terminal_confirmed": False, "order_id": order_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect-readonly", action="store_true")
    args = parser.parse_args(argv)
    if not args.connect_readonly:
        print(json.dumps({"network_started": False, "execution_enabled": False, "provider": "alpaca",
                          "trading_endpoint": TRADING_URL, "live_supported": False}))
        return 0
    try:
        client = AlpacaClient(AlpacaConfig.from_env())
        account, clock = client.get_account(), client.get_clock()
        print(json.dumps({"network_started": True, "execution_enabled": False, "provider": "alpaca",
                          "account_id": account.get("id"), "account_status": account.get("status"),
                          "market_open": clock.get("is_open"), "feed": client.config.feed}))
    except AlpacaError as exc:
        print(json.dumps({"status": "FAILED", "code": exc.code, "http_status": exc.http_status,
                          "execution_enabled": False}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
