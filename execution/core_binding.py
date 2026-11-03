"""ctypes bridge to the single-writer C++ reference trading core (ABI v1)."""
from __future__ import annotations

import ctypes as C
from pathlib import Path


class CoreError(RuntimeError):
    pass


class Config(C.Structure):
    _fields_ = [
        ("abi_version", C.c_uint32), ("struct_size", C.c_uint32),
        ("symbol_locate", C.c_uint32), ("max_order_size", C.c_uint32),
        ("hard_inventory_limit", C.c_int64), ("max_notional_units", C.c_uint64),
        ("max_open_orders", C.c_uint64), ("daily_loss_limit_units", C.c_int64),
        ("daily_drawdown_limit_units", C.c_int64), ("max_market_age_ns", C.c_uint64),
        ("orders_per_second", C.c_uint64), ("price_low", C.c_int64),
        ("price_high", C.c_int64), ("tick_size", C.c_int64),
        ("max_book_orders", C.c_uint64), ("max_trade_records", C.c_uint64),
        ("symbol", C.c_char * 9), ("reserved", C.c_uint8 * 7),
    ]


class Health(C.Structure):
    _fields_ = [(name, C.c_uint8) for name in (
        "broker_healthy", "system_healthy", "feed_connected", "sequence_complete", "live_source"
    )] + [("reserved", C.c_uint8 * 3)]


class Quote(C.Structure):
    _fields_ = [
        ("bid_price", C.c_int64), ("ask_price", C.c_int64),
        ("bid_quantity", C.c_uint32), ("ask_quantity", C.c_uint32),
        ("market_bid", C.c_int64), ("market_ask", C.c_int64),
        ("market_bid_quantity", C.c_uint64), ("market_ask_quantity", C.c_uint64),
        ("last_valid_bbo_ns", C.c_uint64), ("market_fresh", C.c_uint8),
        ("trading_status_valid", C.c_uint8), ("quote_available", C.c_uint8),
        ("reserved", C.c_uint8 * 5),
    ]


class Permit(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("reservation_id", "risk_epoch", "deadline_ns")]


class Snapshot(C.Structure):
    _fields_ = [
        ("risk_epoch", C.c_uint64), ("position", C.c_int64),
        ("buy_reserved", C.c_uint64), ("sell_reserved", C.c_uint64),
        ("open_orders", C.c_uint64), ("net_pnl_units", C.c_int64),
        ("reason", C.c_uint32), ("last_risk_reject", C.c_uint32),
        ("killed", C.c_uint8), ("armed", C.c_uint8),
        ("reconcile_required", C.c_uint8), ("session_complete", C.c_uint8),
        ("pnl_mark_valid", C.c_uint8), ("reserved", C.c_uint8 * 3),
    ]


class Core:
    def __init__(self, library: Path, config: Config):
        if not library.is_file():
            raise CoreError("Compiled libvector_core is unavailable; execution disabled.")
        self.lib = C.CDLL(str(library.resolve()))
        self.handle = C.c_void_p()
        signatures = {
            "vc_create": [C.POINTER(Config), C.POINTER(C.c_void_p)],
            "vc_destroy": [C.c_void_p],
            "vc_last_error": [C.c_void_p, C.c_char_p, C.c_uint32],
            "vc_process_itch": [C.c_void_p, C.POINTER(C.c_uint8), C.c_uint32, C.c_uint64],
            "vc_set_health": [C.c_void_p, C.POINTER(Health)],
            "vc_set_quote_economics": [C.c_void_p, C.c_int64, C.c_int64, C.c_int64, C.c_int64],
            "vc_get_quote": [C.c_void_p, C.c_uint64, C.POINTER(Quote)],
            "vc_get_snapshot": [C.c_void_p, C.POINTER(Snapshot)],
            "vc_arm_flat": [C.c_void_p, C.c_uint8, C.c_uint8, C.c_uint64, C.c_int64, C.c_uint64],
            "vc_check_and_reserve": [C.c_void_p, C.c_uint64, C.c_uint8, C.c_uint32,
                                     C.c_int64, C.c_uint64, C.c_uint64, C.POINTER(Permit)],
            "vc_send_boundary": [C.c_void_p, C.POINTER(Permit), C.c_uint64],
            "vc_fill": [C.c_void_p, C.c_uint64, C.c_uint64, C.c_uint32, C.c_int64],
            "vc_execution_correction": [C.c_void_p],
            "vc_update_pnl": [C.c_void_p, C.c_int64, C.c_int64, C.c_int64, C.c_int64, C.c_uint8],
            "vc_kill": [C.c_void_p, C.c_uint32],
        }
        for name in ("ack", "cancel_request", "cancel_confirm", "reject", "definitely_not_sent", "outcome_unknown"):
            signatures["vc_" + name] = [C.c_void_p, C.c_uint64]
        try:
            for name, args in signatures.items():
                fn = getattr(self.lib, name)
                fn.argtypes = args
                fn.restype = C.c_int
        except AttributeError as exc:
            raise CoreError("Trading core ABI is incomplete; execution disabled.") from exc
        config.abi_version = 1
        config.struct_size = C.sizeof(Config)
        if self.lib.vc_create(C.byref(config), C.byref(self.handle)) != 0:
            raise CoreError("Trading core rejected its configuration.")

    def call(self, name, *args, accepted=(0,)):
        result = getattr(self.lib, "vc_" + name)(self.handle, *args)
        if result not in accepted:
            error = C.create_string_buffer(512)
            self.lib.vc_last_error(self.handle, error, len(error))
            raise CoreError(f"C++ {name} failed ({result}): {error.value.decode(errors='replace')}")
        return result

    def close(self):
        if self.handle:
            self.call("destroy")
            self.handle = C.c_void_p()

    def process_itch(self, message: bytes, now: int):
        data = (C.c_uint8 * len(message)).from_buffer_copy(message)
        self.call("process_itch", data, len(message), now)

    def set_quote_economics(self, fee_per_share: int, minimum_fee: int, slippage: int, minimum_net_edge: int):
        # Validate before ctypes conversion: arbitrarily large Python integers
        # must never silently wrap to a cheaper int64 value at the ABI boundary.
        values = (fee_per_share, minimum_fee, slippage, minimum_net_edge)
        if any(type(value) is not int or not 0 <= value <= 1_000_000_000 for value in values):
            raise CoreError("Quote economics require integer values in 0..1000000000 units.")
        self.call("set_quote_economics", *values)

    def health(self, *, broker: bool, system: bool, feed: bool, sequence: bool, live: bool):
        value = Health(int(broker), int(system), int(feed), int(sequence), int(live))
        self.call("set_health", C.byref(value))

    def quote(self, now: int) -> Quote:
        quote = Quote()
        self.call("get_quote", now, C.byref(quote), accepted=(0, 2))
        return quote

    def snapshot(self) -> Snapshot:
        value = Snapshot()
        self.call("get_snapshot", C.byref(value))
        return value

    def reserve(self, order_id: int, side: int, quantity: int, price: int, now: int, deadline: int):
        permit = Permit()
        code = self.call("check_and_reserve", order_id, side, quantity, price, now, deadline,
                         C.byref(permit), accepted=(0, 2, 3))
        return permit if code == 0 else None

    def send_boundary(self, permit: Permit, now: int):
        self.call("send_boundary", C.byref(permit), now)
