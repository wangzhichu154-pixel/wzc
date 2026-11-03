"""Optional approved numerical model gate. Never creates an order or authority.

The registry is operator-managed. Files are checked at startup and every second;
revocation or a changed active model latches WAIT until process restart. Runtime
features use exchange timestamps, while signal lifetimes use monotonic time.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat


class AIGateError(RuntimeError):
    pass


def market_from_quote(quote):
    return {
        "bid": quote.market_bid, "ask": quote.market_ask,
        "bid_qty": quote.market_bid_quantity, "ask_qty": quote.market_ask_quantity,
        "valid": bool(quote.market_bid > 0 and quote.market_ask > quote.market_bid
                      and quote.market_bid_quantity > 0 and quote.market_ask_quantity > 0),
        "tradable": bool(quote.trading_status_valid),
    }


def fingerprint(market):
    return tuple(market[name] for name in ("bid", "ask", "bid_qty", "ask_qty", "valid", "tradable"))


class AIParticipationGate:
    def __init__(self, store: Path, symbol: str, policy_config):
        from ml.features import FeatureState
        from ml.model import LinearModel
        from ml.registry import load_approved_model
        from ml.runtime import TradingPolicy

        if policy_config.clock != "monotonic" or policy_config.allow_short:
            raise AIGateError("Paper AI requires monotonic signals and verified long-only permissions.")
        self.store, self.symbol, self.config = Path(store), symbol, policy_config
        self._loader = load_approved_model
        approved = self._loader(self.store, mode="paper")
        self._verify_manifest(approved)
        descriptor = os.open(approved.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise AIGateError("Approved model must be a regular JSON file.")
            raw = source.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != approved.sha256:
            raise AIGateError("Approved model file hash mismatch.")
        model = LinearModel.from_bytes(raw)
        if (model.provenance.get("kind") != "licensed_market"
                or model.provenance.get("symbol") != symbol
                or model.model_version != approved.version
                or model.horizon_ns != approved.manifest.get("target_horizon_ns")):
            raise AIGateError("Model identity, provenance or symbol is not approved for Paper.")
        self.version, self.artifact_sha256 = approved.version, approved.sha256
        self.manifest = approved.manifest
        self.features = FeatureState(model.feature_config)
        self.policy = TradingPolicy(model, policy_config)
        self._last_market = None
        self._sample_market = None
        self._last_registry_check_ns = None
        self._latched_reason = ""
        self.last_signal = self._wait(0, 0, "NO_SIGNAL")

    def _verify_manifest(self, approved):
        manifest = approved.manifest
        if (manifest.get("provenance_kind") != "licensed_market"
                or manifest.get("authorized_use_attested") is not True
                or manifest.get("symbol") != self.symbol
                or manifest.get("model_sha256") != approved.sha256
                or manifest.get("version") != approved.version
                or manifest.get("policy") != self.config.policy_document()
                or manifest.get("policy_sha256") != self.config.sha256
                or manifest.get("policy_clock_contract") != "paper:monotonic;simulation:exchange_logical"):
            raise AIGateError("Approved model policy, provenance or clock contract does not match operator configuration.")

    def _wait(self, now, inventory, reason):
        return {"schema_version": 1, "symbol": self.symbol,
                "model_version": getattr(self, "version", ""),
                "artifact_file_sha256": getattr(self, "artifact_sha256", ""),
                "policy_sha256": self.config.sha256, "clock": "monotonic",
                "signal_time_ns": now, "expires_ns": now, "action": "WAIT",
                "allow_bid": False, "allow_ask": False, "target_position": inventory,
                "reason_codes": [reason], "execution_authorized": False}

    def _registry_check(self, now):
        if self._latched_reason:
            return
        if self._last_registry_check_ns is not None and now < self._last_registry_check_ns:
            self._latched_reason = "CLOCK_REGRESSION"
            return
        if self._last_registry_check_ns is not None and now - self._last_registry_check_ns < 1_000_000_000:
            return
        self._last_registry_check_ns = now
        try:
            approved = self._loader(self.store, mode="paper")
            self._verify_manifest(approved)
            if approved.version != self.version or approved.sha256 != self.artifact_sha256:
                raise AIGateError("Active model changed; automatic model replacement is prohibited.")
        except Exception:
            self._latched_reason = "MODEL_REVOKED_OR_CHANGED"

    def observe(self, *, sequence, source_timestamp_ns, market, now_ns, inventory, market_fresh, status_event=False):
        signature = fingerprint(market)
        unchanged = signature == self._last_market
        # FeatureState and offline exporter operate on BBO/status changes only.
        # Unchanged heartbeat/trade traffic must not prolong signal lifetime.
        if unchanged and not status_event:
            return self.current(now_ns=now_ns, inventory=inventory, market=market, market_fresh=market_fresh)
        self._last_market = signature
        try:
            previous_resets = self.features.resets
            sample = self.features.update({"sequence": sequence, "timestamp_ns": source_timestamp_ns, **market})
            if unchanged and sample is None and previous_resets == self.features.resets:
                # A redundant valid status must check causality without
                # canceling or extending the existing signal's original TTL.
                return self.current(now_ns=now_ns, inventory=inventory, market=market, market_fresh=market_fresh)
            self._sample_market = signature if sample is not None else None
            self.last_signal = self.policy.evaluate(sample, symbol=self.symbol, now_ns=now_ns,
                                                   market=market, inventory=inventory, market_fresh=market_fresh)
        except Exception:
            self._latched_reason = "MODEL_OR_FEATURE_FAILURE"
        return self.current(now_ns=now_ns, inventory=inventory, market=market, market_fresh=market_fresh)

    def current(self, *, now_ns, inventory, market, market_fresh):
        self._registry_check(now_ns)
        if self._latched_reason:
            self.last_signal = self._wait(now_ns, inventory, self._latched_reason)
        elif not market_fresh or not market["valid"] or not market["tradable"]:
            self.last_signal = self._wait(now_ns, inventory, "STALE_OR_INVALID_MARKET")
        elif self._sample_market is None:
            self.last_signal = self._wait(now_ns, inventory, "FEATURE_WARMUP_OR_RESET")
        elif fingerprint(market) != self._sample_market:
            self.last_signal = self._wait(now_ns, inventory, "FEATURE_MARKET_MISMATCH")
        else:
            try:
                self.last_signal = self.policy.current(now_ns=now_ns, inventory=inventory, market_fresh=market_fresh)
            except Exception:
                self._latched_reason = "MODEL_OR_FEATURE_FAILURE"
                self.last_signal = self._wait(now_ns, inventory, self._latched_reason)
        self.last_signal["artifact_file_sha256"] = self.artifact_sha256
        return dict(self.last_signal)
