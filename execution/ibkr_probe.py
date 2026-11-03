"""Operator-only IBKR read-only snapshot using the official optional ibapi SDK.

No order placement, cancellation, or modification exists in this module.
The output is diagnostic evidence, not an atomic ledger reconciliation or a
permission to enable trading. No Web endpoint imports or launches this tool.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Configuration:
    account: str
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 71
    timeout: float = 20.0
    account_kind: str = "paper"
    mode: str = "readonly"

    def validate(self) -> None:
        if self.mode != "readonly":
            raise ProbeError("Execution is unavailable: this tool is read-only.")
        if not self.account or len(self.account) > 64 or not self.account.isalnum():
            raise ProbeError("An explicit valid IBKR_ACCOUNT is required.")
        if not self.host or not 1 <= self.port <= 65535:
            raise ProbeError("Invalid gateway host or port.")
        if not 1 <= self.client_id <= 2_147_483_647:
            raise ProbeError("Use an explicit nonzero client ID; client 0 is prohibited.")
        if not 1 <= self.timeout <= 120:
            raise ProbeError("Timeout must be between 1 and 120 seconds.")
        if self.account_kind not in {"paper", "live"}:
            raise ProbeError("Unknown account kind.")
        if self.account_kind == "paper" and not self.account.startswith("DU"):
            raise ProbeError("Paper mode requires a DU account as a preliminary safeguard.")
        if self.account_kind == "live" and self.account.startswith("DU"):
            raise ProbeError("Configured live account resembles a paper account.")

    def validate_managed_accounts(self, accounts: str) -> None:
        available = {value.strip() for value in accounts.split(",") if value.strip()}
        if self.account not in available:
            raise ProbeError("Configured account is not reported by this Gateway session.")


class Snapshot:
    """Bounded diagnostic collector. It never computes positions from fills."""

    MAX_RECORDS = 100_000

    def __init__(self, account: str):
        self.account = account
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.account_values: dict[str, dict[str, Any]] = {}
        self.warnings: list[dict[str, Any]] = []
        self.server_time: int | None = None
        self.started_at = datetime.now(timezone.utc).isoformat()

    def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        records = getattr(self, collection)
        existing = records.get(key)
        if collection == "executions" and existing is not None and existing != value:
            raise ProbeError("Conflicting duplicate execution identity; manual review required.")
        if key not in records and len(records) >= self.MAX_RECORDS:
            raise ProbeError("Snapshot capacity exceeded; refusing incomplete reconciliation.")
        records[key] = value

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mode": "readonly",
            "account": self.account,
            "started_at": self.started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "server_time": self.server_time,
            "query_completed": True,
            "trading_enabled": False,
            "account_mode_attested": False,
            "atomic_snapshot": False,
            "ledger_reconciled": False,
            "execution_history_complete": False,
            "positions": list(self.positions.values()),
            "open_orders": list(self.orders.values()),
            "executions": list(self.executions.values()),
            "account_values": list(self.account_values.values()),
            "warnings": self.warnings,
            "limitations": [
                "IBKR queries are asynchronous, not an atomic account snapshot.",
                "Execution history availability depends on Gateway/API permissions and retention.",
                "Execution corrections retain their full identity; this probe does not apply ledger adjustments.",
                "Account prefix and port do not independently attest paper/live mode.",
                "No orders have been placed, canceled, or modified by this tool.",
            ],
        }


def load_sdk():
    try:
        client = importlib.import_module("ibapi.client")
        wrapper = importlib.import_module("ibapi.wrapper")
        execution = importlib.import_module("ibapi.execution")
        return client.EClient, wrapper.EWrapper, execution.ExecutionFilter
    except ImportError as exc:
        raise ProbeError(
            "Official ibapi SDK unavailable. Install an operator-approved IBKR API release; "
            "execution remains disabled."
        ) from exc


def query_gateway(config: Configuration) -> dict[str, Any]:
    config.validate()
    EClient, EWrapper, ExecutionFilter = load_sdk()
    state = Snapshot(config.account)
    end_events = {name: threading.Event() for name in (
        "handshake", "accounts", "positions", "orders", "executions", "summary", "time"
    )}
    failed = threading.Event()
    errors: list[str] = []
    stop_requested = threading.Event()

    def fail(message: str) -> None:
        if not failed.is_set():
            errors.append(message)
            failed.set()

    def save(collection: str, key: str, value: dict[str, Any]) -> None:
        try:
            state.put(collection, key, value)
        except ProbeError as exc:
            fail(str(exc))

    class ReadOnlyClient(EWrapper, EClient):
        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self, self)

        def nextValidId(self, orderId):  # noqa: N802 — official SDK callback
            # The ID is only a handshake signal; it is never allocated or used.
            end_events["handshake"].set()

        def managedAccounts(self, accountsList):
            try:
                config.validate_managed_accounts(accountsList)
                end_events["accounts"].set()
            except ProbeError as exc:
                fail(str(exc))

        def currentTime(self, value):
            state.server_time = int(value)
            end_events["time"].set()

        def accountSummary(self, reqId, account, tag, value, currency):
            if reqId == 9001 and account == config.account:
                save("account_values", f"{tag}:{currency}", {
                    "tag": tag, "value": str(value), "currency": currency,
                })

        def accountSummaryEnd(self, reqId):
            if reqId == 9001:
                end_events["summary"].set()

        def position(self, account, contract, pos, avgCost):
            if account == config.account:
                key = f"{contract.conId}:{contract.currency}:{contract.secType}"
                save("positions", key, {
                    "contract_id": contract.conId, "symbol": contract.symbol,
                    "security_type": contract.secType, "currency": contract.currency,
                    "quantity": str(pos), "average_cost": str(avgCost),
                })

        def positionEnd(self):
            end_events["positions"].set()

        def openOrder(self, orderId, contract, order, orderState):
            if not order.account:
                fail("An open order has no account identity; ownership cannot be verified.")
                return
            if order.account == config.account:
                key = f"{order.clientId}:{orderId}:{order.permId}"
                save("orders", key, {
                    "order_id": orderId, "client_id": order.clientId,
                    "perm_id": order.permId, "contract_id": contract.conId,
                    "symbol": contract.symbol, "action": order.action,
                    "order_type": order.orderType, "quantity": str(order.totalQuantity),
                    "limit_price": str(order.lmtPrice), "status": orderState.status,
                })

        def openOrderEnd(self):
            end_events["orders"].set()

        def execDetails(self, reqId, contract, execution):
            if reqId == 9002 and execution.acctNumber == config.account:
                if not execution.execId:
                    fail("Execution report has no stable identity.")
                    return
                save("executions", execution.execId, {
                    "execution_id": execution.execId,
                    "order_id": execution.orderId,
                    "client_id": execution.clientId,
                    "perm_id": execution.permId,
                    "contract_id": contract.conId,
                    "symbol": contract.symbol,
                    "time": execution.time,
                    "side": execution.side,
                    "quantity": str(execution.shares),
                    "price": str(execution.price),
                    "cumulative_quantity": str(execution.cumQty),
                })

        def execDetailsEnd(self, reqId):
            if reqId == 9002:
                end_events["executions"].set()

        def connectionClosed(self):
            if not stop_requested.is_set():
                fail("Gateway connection closed before snapshot completion.")

        def error(self, reqId, *args):
            # Supports older (code, message, advancedReject) and newer
            # (errorTime, code, message, advancedReject) SDK callback shapes.
            if len(args) >= 3 and isinstance(args[1], int):
                code, message = args[1], str(args[2])
            elif len(args) >= 2:
                code, message = args[0], str(args[1])
            else:
                fail("Unrecognized SDK error callback.")
                return
            if code in {2104, 2106, 2107, 2108, 2158}:
                if len(state.warnings) < 100:
                    state.warnings.append({"code": code, "message": message[:500]})
            else:
                fail(f"IBKR query failed with code {code}; inspect Gateway diagnostics.")

    client = ReadOnlyClient()
    deadline = time.monotonic() + config.timeout
    previous_timeout = socket.getdefaulttimeout()

    def await_events(names: tuple[str, ...]) -> None:
        while not all(end_events[name].is_set() for name in names):
            if failed.is_set():
                raise ProbeError(errors[0])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                missing = [name for name in names if not end_events[name].is_set()]
                raise ProbeError("Incomplete snapshot: timeout awaiting " + ", ".join(missing))
            failed.wait(min(remaining, 0.05))
        if failed.is_set():
            raise ProbeError(errors[0])

    thread = None
    try:
        socket.setdefaulttimeout(min(config.timeout, 5.0))
        client.connect(config.host, config.port, clientId=config.client_id)
        socket.setdefaulttimeout(previous_timeout)
        if not client.isConnected():
            raise ProbeError("Gateway connection was not established.")

        def reader():
            try:
                client.run()
            except Exception:
                fail("SDK reader failed; snapshot is not usable.")

        thread = threading.Thread(target=reader, name="ibkr-readonly-reader", daemon=True)
        thread.start()
        await_events(("handshake", "accounts"))
        client.reqCurrentTime()
        client.reqAccountSummary(9001, "All", "NetLiquidation,TotalCashValue,GrossPositionValue,AvailableFunds")
        client.reqPositions()
        client.reqAllOpenOrders()
        execution_filter = ExecutionFilter()
        execution_filter.acctCode = config.account
        client.reqExecutions(9002, execution_filter)
        await_events(("positions", "orders", "executions", "summary", "time"))
    except ProbeError:
        raise
    except Exception as exc:
        raise ProbeError("SDK connection/query failed; no trading permission was granted.") from exc
    finally:
        socket.setdefaulttimeout(previous_timeout)
        stop_requested.set()
        try:
            client.disconnect()
        except Exception:
            fail("SDK disconnect failed; snapshot discarded.")
        if thread:
            thread.join(timeout=2.0)
    if thread and thread.is_alive():
        raise ProbeError("SDK reader did not stop cleanly; snapshot discarded.")
    if failed.is_set():
        raise ProbeError(errors[0])
    return state.as_dict()


def write_private_snapshot(path: Path, data: dict[str, Any]) -> None:
    # Exclusive creation prevents following a destination symlink or replacing
    # an existing operator artifact. The output contains private account data.
    encoded = (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default=os.environ.get("IBKR_ACCOUNT", ""))
    parser.add_argument("--host", default=os.environ.get("IBKR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("IBKR_PORT", "7497"))
    parser.add_argument("--client-id", type=int, default=os.environ.get("IBKR_CLIENT_ID", "71"))
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--account-kind", choices=("paper", "live"), default="paper")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    config = Configuration(
        account=args.account, host=args.host, port=args.port,
        client_id=args.client_id, timeout=args.timeout, account_kind=args.account_kind,
        mode=os.environ.get("IBKR_EXECUTION_MODE", "readonly"),
    )
    try:
        result = query_gateway(config)
        write_private_snapshot(args.output, result)
    except (ProbeError, OSError, ValueError) as exc:
        print(json.dumps({"success": False, "code": "IBKR_UNAVAILABLE", "message": str(exc),
                          "trading_enabled": False}), file=sys.stderr)
        return 2
    print(json.dumps({"success": True, "code": "READ_ONLY_QUERY_COMPLETED",
                      "output": str(args.output), "trading_enabled": False,
                      "ledger_reconciled": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
