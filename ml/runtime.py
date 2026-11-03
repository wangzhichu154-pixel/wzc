"""Fail-closed, side-filter-only numerical model policy.

This module cannot construct an order, change broker configuration, or raise risk
limits. Estimated mid returns are not probabilities or promises of executable PnL.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
import re

from .features import FEATURE_NAMES, FeatureSample
from .model import canonical_bytes


@dataclass(frozen=True)
class PolicyConfig:
    signal_ttl_ns: int = 250_000_000
    min_expected_edge_bps: float = 1.0
    fee_per_share_units: int = 35
    min_fee_per_order_units: int = 10_000
    slippage_buffer_units: int = 20
    max_target_position: int = 10
    allow_short: bool = False
    clock: str = "monotonic"

    def __post_init__(self):
        for name in ("signal_ttl_ns", "fee_per_share_units", "min_fee_per_order_units",
                     "slippage_buffer_units", "max_target_position"):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 10**12:
                raise ValueError(f"Invalid policy bound: {name}")
        if not 1_000_000 <= self.signal_ttl_ns <= 1_000_000_000 or not 1 <= self.max_target_position <= 1_000_000:
            raise ValueError("Unsupported signal lifetime or position cap")
        if type(self.allow_short) is not bool or self.clock not in ("monotonic", "exchange_logical"):
            raise ValueError("Invalid policy permission or clock domain")
        if type(self.min_expected_edge_bps) not in (float, int) or not math.isfinite(self.min_expected_edge_bps) or not 0 <= self.min_expected_edge_bps <= 10_000:
            raise ValueError("Invalid cost buffer")

    def policy_document(self):
        # Clock domains differ offline and online; economic/permission policy does not.
        return {key: value for key, value in asdict(self).items() if key != "clock"}

    @property
    def sha256(self):
        return hashlib.sha256(canonical_bytes(self.policy_document())).hexdigest()


class TradingPolicy:
    def __init__(self, model, policy_config: PolicyConfig):
        if policy_config.signal_ttl_ns > model.horizon_ns:
            raise ValueError("Signal lifetime cannot exceed prediction horizon")
        self.model, self.config = model, policy_config
        self.model_sha256 = getattr(model, "artifact_file_sha256", None)
        self.model_content_sha256 = hashlib.sha256(canonical_bytes(model.to_dict())).hexdigest()
        self._cached = None
        self._last_key = None
        self._last_now = None
        self._symbol = None
        self._market_fingerprint = None

    def _base(self, symbol, now_ns, inventory, reasons):
        return {"schema_version": 1, "symbol": symbol, "signal_time_ns": now_ns,
                "expires_ns": now_ns, "clock": self.config.clock,
                "source_timestamp_ns": None, "source_sequence": None,
                "prediction_horizon_ns": self.model.horizon_ns,
                "model_version": self.model.model_version, "model_sha256": self.model_sha256,
                "model_content_sha256": self.model_content_sha256,
                "policy_sha256": self.config.sha256, "provenance_kind": self.model.provenance["kind"],
                "direction": "neutral", "predicted_return_bps": None,
                "action": "WAIT", "target_position": inventory,
                "allow_bid": False, "allow_ask": False, "reason_codes": reasons,
                "inputs": {}, "required_edge_bps": None,
                "invalidation_conditions": ["TTL_EXPIRED", "MARKET_INVALID", "RISK_REJECTED",
                                              "MODEL_REVOKED", "ACCOUNT_UNHEALTHY"],
                "execution_authorized": False}

    def _wait(self, symbol, now_ns, inventory, reason):
        self._cached = self._base(symbol, now_ns, inventory, [reason])
        return dict(self._cached)

    def evaluate(self, sample: FeatureSample | None, *, symbol: str, now_ns: int,
                 market: dict, inventory: int, market_fresh: bool = True,
                 _defer_inventory: bool = False) -> dict:
        if _defer_inventory and self.config.clock != "exchange_logical":
            raise ValueError("Only offline simulation may defer inventory checks")
        if type(now_ns) is not int or now_ns < 0 or type(inventory) is not int:
            raise ValueError("Invalid decision clock or inventory")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol):
            raise ValueError("Invalid trading symbol")
        if self._symbol is not None and symbol != self._symbol:
            return self._wait(symbol, now_ns, inventory, "SYMBOL_MISMATCH")
        self._symbol = symbol
        if self._last_now is not None and now_ns < self._last_now:
            return self._wait(symbol, now_ns, inventory, "CLOCK_REGRESSION")
        self._last_now = now_ns
        if not market_fresh:
            return self._wait(symbol, now_ns, inventory, "STALE_MARKET")
        if sample is None:
            return self._wait(symbol, now_ns, inventory, "FEATURE_WARMUP_OR_RESET")
        if not isinstance(market, dict) or market.get("valid") not in (True, 1) or market.get("tradable") not in (True, 1):
            return self._wait(symbol, now_ns, inventory, "MARKET_INVALID")
        if any(type(market.get(k)) is not int or not 0 < market[k] <= 10**12 for k in ("bid", "ask", "bid_qty", "ask_qty")) or market["ask"] <= market["bid"]:
            return self._wait(symbol, now_ns, inventory, "MARKET_INVALID")
        mid = (market["bid"] + market["ask"]) / 2
        if sample.mid_units != mid:
            return self._wait(symbol, now_ns, inventory, "FEATURE_MARKET_MISMATCH")
        key = (sample.sequence, sample.timestamp_ns)
        fingerprint = tuple(market[name] for name in ("bid", "ask", "bid_qty", "ask_qty"))
        if self._last_key is not None and key[0] <= self._last_key[0]:
            if key == self._last_key:
                if fingerprint != self._market_fingerprint:
                    return self._wait(symbol, now_ns, inventory, "FEATURE_MARKET_MISMATCH")
                return self.current(now_ns=now_ns, inventory=inventory, market_fresh=market_fresh)
            return self._wait(symbol, now_ns, inventory, "SOURCE_REGRESSION")
        self._last_key = key
        self._market_fingerprint = fingerprint
        result = self._base(symbol, now_ns, inventory, [])
        try:
            prediction = self.model.predict(sample.features)
            if not math.isfinite(prediction):
                raise ValueError("Nonfinite prediction")
        except (ValueError, TypeError, ArithmeticError):
            return self._wait(symbol, now_ns, inventory, "MODEL_INVALID")
        # Conservative one-share minimum commission and round-trip spread/slip.
        # Final executable quote economics and order risk are checked independently.
        costs = (market["ask"] - market["bid"] + 2 * (self.config.fee_per_share_units +
                 self.config.min_fee_per_order_units + self.config.slippage_buffer_units)) / mid * 10_000
        required = costs + self.config.min_expected_edge_bps
        result.update(expires_ns=now_ns + self.config.signal_ttl_ns,
                      source_timestamp_ns=sample.timestamp_ns, source_sequence=sample.sequence,
                      predicted_return_bps=prediction, required_edge_bps=required,
                      direction="up" if prediction > 0 else "down" if prediction < 0 else "neutral",
                      inputs={name: value for name, value in zip(FEATURE_NAMES, sample.features)})
        if abs(prediction) <= required:
            result["reason_codes"] = ["EXPECTED_EDGE_BELOW_COST"]
        elif prediction > 0:
            result.update(allow_bid=True, action="ALLOW_BID", target_position=self.config.max_target_position)
        elif inventory > 0 or self.config.allow_short or _defer_inventory:
            result.update(allow_ask=True, action="ALLOW_ASK",
                          target_position=-self.config.max_target_position if self.config.allow_short else 0)
        else:
            result["reason_codes"] = ["SHORT_NOT_PERMITTED"]
        self._cached = result
        if _defer_inventory:
            result["inventory_check_deferred_to_simulator"] = True
            return dict(result)
        return self.current(now_ns=now_ns, inventory=inventory, market_fresh=market_fresh)

    def research_participation(self, sample, *, symbol, now_ns, market):
        """Direction-only file for C++ replay; C++ must independently check inventory.

        Never infer an account position from historical public market prints.
        This is unavailable in the monotonic broker clock domain.
        """
        return self.evaluate(sample, symbol=symbol, now_ns=now_ns, market=market,
                             inventory=0, _defer_inventory=True)

    def current(self, *, now_ns: int, inventory: int, market_fresh: bool = True) -> dict:
        if type(now_ns) is not int or now_ns < 0 or type(inventory) is not int:
            raise ValueError("Invalid clock or inventory")
        if self._cached is None:
            return self._base("UNKNOWN", now_ns, inventory, ["NO_SIGNAL"])
        if self._last_now is not None and now_ns < self._last_now:
            return self._wait(self._cached["symbol"], now_ns, inventory, "CLOCK_REGRESSION")
        self._last_now = now_ns
        result = dict(self._cached)
        result["reason_codes"] = list(result["reason_codes"])
        if now_ns < result["signal_time_ns"] or now_ns >= result["expires_ns"] or not market_fresh:
            reason = "CLOCK_REGRESSION" if now_ns < result["signal_time_ns"] else "STALE_MARKET" if not market_fresh else "TTL_EXPIRED"
            result.update(allow_bid=False, allow_ask=False, action="WAIT", target_position=inventory, reason_codes=[reason])
        elif (result["allow_bid"] and inventory >= self.config.max_target_position) or (result["allow_ask"] and (inventory <= -self.config.max_target_position or (not self.config.allow_short and inventory <= 0))):
            result.update(allow_bid=False, allow_ask=False, action="WAIT", target_position=inventory,
                          reason_codes=["TARGET_REACHED_OR_PERMISSION_DENIED"])
        return result
