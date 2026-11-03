"""Strict account valuation and aggregation of actual C++ simulation ledgers.

Quote freshness is not account solvency. A reconciled, flat terminal ledger can
be valued after the market closes, but cannot repair an unobserved equity path.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path


VALUATION_BASES = {"fresh_bbo", "reconciled_flat_ledger", "unavailable"}
RESEARCH_SOURCE_FILES = ("backtest.py", "features.py", "runtime.py", "model.py", "data.py", "valuation.py")


def research_source_fingerprints():
    """Bind admission to the installed research code, not caller supplied hashes."""
    directory = Path(__file__).parent
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in RESEARCH_SOURCE_FILES}


def evaluation_contract():
    """Fixed research execution assumptions, committed before holdout replay.

    Changing this contract requires new evidence on an unused test interval.
    These are simulator assumptions, not measured broker latency or live limits.
    """
    return {"schema_version": 1,
            "scenarios": ["model", "cost_aware_two_sided", "wait", "double_cost", "fivefold_latency"],
            "maximum_order_shares": 1, "daily_loss_limit_cents": 10000, "new_order_rate": 5,
            "latency_ns": 1_000_000, "cancel_latency_ns": 1_000_000,
            "quote_lifetime_ns": 1_000_000_000, "tick_size": 100, "min_net_edge_units": 0,
            "cost_stress_multiplier": 2, "latency_stress_multiplier": 5}


def _integer(value, name, *, nonnegative=False):
    if type(value) is not int or not -(2**63) <= value < 2**63 or nonnegative and value < 0:
        raise ValueError(f"Missing or invalid ledger integer: {name}")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise ValueError(f"Missing or invalid ledger boolean: {name}")
    return value


def validate_valuation(simulation):
    """Reconcile monetary, quantity and valuation invariants before reporting.

    The binary/input binding proves which controlled engine produced the report;
    these checks additionally reject inconsistent flags, cash and aggregate data.
    They are not a signature against an owner able to rewrite every evidence file.
    """
    if not isinstance(simulation, dict):
        raise ValueError("Simulation ledger must be an object")
    for name in ("realized_pnl_units", "unrealized_pnl_units", "net_pnl_units", "cash_before_fees_units", "inventory"):
        _integer(simulation.get(name), name)
    for name in ("fill_count", "filled_quantity", "fees_units", "rebates_units", "drawdown_units", "open_orders_at_end"):
        _integer(simulation.get(name), name, nonnegative=True)
    quote_valid = _boolean(simulation.get("pnl_mark_valid"), "pnl_mark_valid")
    _boolean(simulation.get("killed"), "killed")
    realized, fees, rebates = (simulation[k] for k in ("realized_pnl_units", "fees_units", "rebates_units"))
    if simulation["net_pnl_units"] != realized + simulation["unrealized_pnl_units"] - fees + rebates:
        raise ValueError("Legacy PnL identity is inconsistent")
    execution = simulation.get("execution_metrics")
    if not isinstance(execution, dict):
        raise ValueError("Execution ledger is required")
    for name in ("buy_quantity", "sell_quantity", "buy_notional_units", "sell_notional_units", "turnover_units",
                 "closed_inventory_episodes", "holding_total_ns", "peak_abs_inventory_shares"):
        _integer(execution.get(name), name, nonnegative=True)
    exposure = execution.get("inventory_share_seconds")
    if type(exposure) not in (int, float) or not math.isfinite(exposure) or exposure < 0:
        raise ValueError("Inventory exposure must be a finite nonnegative measurement")
    if (execution["buy_quantity"] + execution["sell_quantity"] != simulation["filled_quantity"]
            or execution["buy_quantity"] - execution["sell_quantity"] != simulation["inventory"]
            or execution["buy_notional_units"] + execution["sell_notional_units"] != execution["turnover_units"]
            or execution["sell_notional_units"] - execution["buy_notional_units"] != simulation["cash_before_fees_units"]
            or simulation["fill_count"] > simulation["filled_quantity"]):
        raise ValueError("Execution quantities, cash or turnover do not reconcile")
    if simulation["inventory"] == 0 and simulation["cash_before_fees_units"] != realized:
        raise ValueError("Flat inventory requires FIFO realized PnL to equal execution cash")
    if simulation["filled_quantity"] == 0 and any((simulation["fill_count"], realized, fees, rebates,
                                                   execution["turnover_units"], simulation["cash_before_fees_units"])):
        raise ValueError("Unfilled account cannot have execution cash, fees or realized PnL")
    value = simulation.get("valuation")
    required = {"schema_version", "basis", "quote_mark_valid", "net_pnl_valid", "unrealized_pnl_units",
                "net_pnl_units", "drawdown_complete", "max_drawdown_units"}
    if not isinstance(value, dict) or set(value) != required or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Explicit supported account valuation evidence is required")
    basis = value["basis"]
    if basis not in VALUATION_BASES:
        raise ValueError("Unsupported account valuation basis")
    if _boolean(value["quote_mark_valid"], "quote_mark_valid") != quote_valid:
        raise ValueError("Valuation quote flag contradicts engine quote evidence")
    valid = _boolean(value["net_pnl_valid"], "net_pnl_valid")
    complete = _boolean(value["drawdown_complete"], "drawdown_complete")
    if basis == "unavailable":
        if quote_valid or valid or complete or any(value[k] is not None for k in ("unrealized_pnl_units", "net_pnl_units", "max_drawdown_units")):
            raise ValueError("Unavailable valuation cannot claim valid monetary results")
        return dict(value)
    if not valid:
        raise ValueError("A numerical valuation requires an explicit valid account basis")
    unrealized = _integer(value["unrealized_pnl_units"], "valuation.unrealized_pnl_units")
    net = _integer(value["net_pnl_units"], "valuation.net_pnl_units")
    if net != realized + unrealized - fees + rebates:
        raise ValueError("Account valuation PnL identity failed")
    if basis == "fresh_bbo":
        if not quote_valid or unrealized != simulation["unrealized_pnl_units"] or net != simulation["net_pnl_units"]:
            raise ValueError("Fresh BBO valuation differs from its marked ledger")
    elif (quote_valid or simulation["inventory"] != 0 or simulation["open_orders_at_end"] != 0
          or unrealized != 0 or simulation["cash_before_fees_units"] != realized):
        raise ValueError("Flat-ledger valuation requires reconciled cash and no inventory or pending orders")
    if complete:
        drawdown = _integer(value["max_drawdown_units"], "valuation.max_drawdown_units", nonnegative=True)
        if drawdown < max(0, -net):
            raise ValueError("Complete drawdown cannot be below the loss from initial zero equity")
    elif value["max_drawdown_units"] is not None:
        raise ValueError("An incomplete equity path cannot produce a complete drawdown")
    return dict(value)


def aggregate_sessions(sessions):
    """Aggregate independently flat-started sessions without netting exposure."""
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("At least one complete session ledger is required")
    values, valuations, identities = [], [], set()
    for item in sessions:
        if not isinstance(item, dict) or not isinstance(item.get("capture_id"), str) or item["capture_id"] in identities:
            raise ValueError("Session identities must be distinct")
        identities.add(item["capture_id"])
        _integer(item.get("events"), "events", nonnegative=True)
        values.append(item["simulation"])
        valuations.append(validate_valuation(item["simulation"]))
    def total(name):
        return sum(row[name] for row in values)
    net_valid = all(value["net_pnl_valid"] for value in valuations)
    quote_valid = all(value["quote_mark_valid"] for value in valuations)
    complete = net_valid and all(value["drawdown_complete"] for value in valuations)
    bases = {value["basis"] for value in valuations}
    basis = next(iter(bases)) if len(bases) == 1 else "mixed_valid" if net_valid else "unavailable"
    if not net_valid:
        basis = "unavailable"
    net = sum(value["net_pnl_units"] for value in valuations) if net_valid else None
    unrealized = sum(value["unrealized_pnl_units"] for value in valuations) if net_valid else None
    drawdown = max(value["max_drawdown_units"] for value in valuations) if complete else None
    account = {"schema_version": 1, "basis": basis, "quote_mark_valid": quote_valid,
               "net_pnl_valid": net_valid, "unrealized_pnl_units": unrealized, "net_pnl_units": net,
               "drawdown_complete": complete, "max_drawdown_units": drawdown}
    episodes = sum(row["execution_metrics"]["closed_inventory_episodes"] for row in values)
    holding = sum(row["execution_metrics"]["holding_total_ns"] for row in values)
    return {"events": sum(item["events"] for item in sessions), "fills": total("fill_count"),
            "fill_count": total("fill_count"), "filled_quantity": total("filled_quantity"),
            "net_pnl_units": net, "realized_pnl_units": total("realized_pnl_units"),
            "unrealized_pnl_units": unrealized, "fees_units": total("fees_units"), "rebates_units": total("rebates_units"),
            "cash_before_fees_units": total("cash_before_fees_units"), "max_drawdown_units": drawdown, "drawdown_units": drawdown,
            "drawdown_scope": "maximum within any independently flat-started session; not a continuous portfolio curve",
            "inventory": total("inventory"), "residual_inventory_by_session": {item["capture_id"]: item["simulation"]["inventory"] for item in sessions},
            "open_orders_at_end": total("open_orders_at_end"), "pnl_mark_valid": quote_valid,
            "valuation": account,
            "open_exposure": any(row["inventory"] != 0 or row["open_orders_at_end"] != 0 for row in values),
            "killed": any(row["killed"] for row in values),
            "turnover_units": sum(row["execution_metrics"]["turnover_units"] for row in values),
            "turnover_ratio": None, "return_on_capital": None,
            "capital_basis": "No supplied account capital; no fabricated return percentage",
            "mean_closed_holding_ns": holding / episodes if episodes else None, "closed_inventory_episodes": episodes,
            "peak_abs_inventory_shares": max(row["execution_metrics"]["peak_abs_inventory_shares"] for row in values),
            "inventory_share_seconds": sum(row["execution_metrics"]["inventory_share_seconds"] for row in values),
            "currency_units": "1/10000 USD", "sessions": len(sessions)}
