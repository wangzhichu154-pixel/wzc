"""Causal, dependency-free book features shared by training and execution.

No news, account state, future message, or future quote is used. Equal exchange
timestamps retain feed sequence order: only the first eligible observation can
emit a decision; later observations update history for a later timestamp.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math

FEATURE_SCHEMA_VERSION = "causal_book_v1"
FEATURE_NAMES = ("spread_bps", "imbalance", "microprice_offset_bps", "past_return_bps",
                 "realized_volatility_bps", "log_depth")
REGULAR_OPEN_NS = (9 * 3600 + 30 * 60) * 1_000_000_000
REGULAR_CLOSE_NS = 16 * 3600 * 1_000_000_000


@dataclass(frozen=True)
class FeatureConfig:
    window_events: int = 32
    max_gap_ns: int = 1_000_000_000
    regular_open_ns: int = REGULAR_OPEN_NS
    regular_close_ns: int = REGULAR_CLOSE_NS

    def validate(self):
        for value in asdict(self).values():
            if type(value) is not int:
                raise ValueError("Feature configuration must use integer bounds")
        if not 4 <= self.window_events <= 1024 or not 1_000_000 <= self.max_gap_ns <= 60_000_000_000:
            raise ValueError("Unsupported feature window or freshness bound")
        if not 0 <= self.regular_open_ns < self.regular_close_ns <= 86_400_000_000_000:
            raise ValueError("Invalid regular trading window")
        return self

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(asdict(cls())):
            raise ValueError("Unsupported feature configuration schema")
        return cls(**value).validate()


@dataclass(frozen=True)
class FeatureSample:
    sequence: int
    timestamp_ns: int
    mid_units: float
    features: tuple[float, ...]


def _integer(row, name, *, low=0, high=2**63 - 1):
    value = row.get(name)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Invalid market observation field: {name}")
    return value


class FeatureState:
    def __init__(self, config: FeatureConfig | None = None):
        self.config = (config or FeatureConfig()).validate()
        self.history = deque(maxlen=self.config.window_events)
        self.last_sequence = None
        self.last_timestamp_ns = None
        self.last_emission_ns = None
        self.last_valid_timestamp_ns = None
        self.last_changed_timestamp_ns = None
        self.last_book = None
        self.resets = 0

    def reset(self):
        """Reset a whole session; callers must do this at an exchange-date change."""
        self.history.clear()
        self.last_sequence = self.last_timestamp_ns = self.last_emission_ns = None
        self.last_valid_timestamp_ns = None
        self.last_changed_timestamp_ns = None
        self.last_book = None
        self.resets += 1

    def _reset_history(self):
        self.history.clear()
        self.last_valid_timestamp_ns = None
        self.last_changed_timestamp_ns = None
        self.last_book = None
        self.resets += 1

    def update(self, observation: dict) -> FeatureSample | None:
        if not isinstance(observation, dict):
            raise ValueError("Market observation must be an object")
        seq = _integer(observation, "sequence", low=1)
        now = _integer(observation, "timestamp_ns", high=86_400_000_000_000 - 1)
        if self.last_sequence is not None and seq <= self.last_sequence:
            self._reset_history()
            raise ValueError("Market sequence must increase strictly")
        if self.last_timestamp_ns is not None and now < self.last_timestamp_ns:
            self._reset_history()
            raise ValueError("Market timestamp regressed; causality cannot be established")
        self.last_sequence, self.last_timestamp_ns = seq, now
        valid = observation.get("valid")
        if type(valid) not in (bool, int) or valid not in (False, True, 0, 1):
            self._reset_history()
            raise ValueError("Market validity flag is required")
        tradable = observation.get("tradable")
        if type(tradable) not in (bool, int) or tradable not in (False, True, 0, 1):
            self._reset_history()
            raise ValueError("Invalid trading status flag")
        if not valid or not tradable or not self.config.regular_open_ns <= now < self.config.regular_close_ns:
            self._reset_history()
            return None
        try:
            bid = _integer(observation, "bid", low=1, high=10**12)
            ask = _integer(observation, "ask", low=1, high=10**12)
            bq = _integer(observation, "bid_qty", low=1, high=10**12)
            aq = _integer(observation, "ask_qty", low=1, high=10**12)
        except ValueError:
            self._reset_history()
            raise
        if ask <= bid:
            self._reset_history()
            raise ValueError("Locked or crossed market cannot produce a feature")
        book = (bid, ask, bq, aq)
        # Ignore unchanged books before gap handling. The online adapter sees
        # trade/heartbeat messages which the BBO-only exporter intentionally
        # omits; those messages must not restart or advance the feature window.
        if book == self.last_book:
            return None
        if self.last_valid_timestamp_ns is not None and now - self.last_valid_timestamp_ns > self.config.max_gap_ns:
            self._reset_history()
        same_timestamp = self.last_changed_timestamp_ns == now
        self.last_changed_timestamp_ns = now
        self.last_valid_timestamp_ns, self.last_book = now, book
        mid = (bid + ask) / 2.0
        self.history.append((now, mid))
        if len(self.history) < self.config.window_events or self.last_emission_ns == now or same_timestamp:
            return None
        # Do not emit the later row of a timestamp merely because it completed
        # warmup. Every timestamp has at most one decision, at its first row.
        mids = [entry[1] for entry in self.history]
        returns = [10_000.0 * (after / before - 1.0) for before, after in zip(mids, mids[1:])]
        mean_return = math.fsum(returns) / len(returns)
        variance = math.fsum((r - mean_return) ** 2 for r in returns) / len(returns)
        micro = (ask * bq + bid * aq) / (bq + aq)
        vector = ((ask - bid) / mid * 10_000.0, (bq - aq) / (bq + aq),
                  (micro - mid) / mid * 10_000.0, (mid / mids[0] - 1.0) * 10_000.0,
                  math.sqrt(variance), math.log1p(bq + aq))
        if not all(math.isfinite(v) for v in vector):
            self._reset_history()
            raise ValueError("Feature arithmetic is not finite")
        self.last_emission_ns = now
        return FeatureSample(seq, now, mid, vector)
