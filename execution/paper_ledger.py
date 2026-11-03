"""Durable broker identities and exact-price FIFO accounting for one paper run."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import time
from collections import deque
from pathlib import Path


class LedgerFault(RuntimeError):
    pass


class PnLLedger:
    def __init__(self):
        self.lots: deque[list[int]] = deque()
        self.realized = 0
        self.fees: dict[str, int] = {}

    @property
    def position(self):
        return sum(lot[0] for lot in self.lots)

    def fill(self, execution_id: str, signed_quantity: int, price: int, estimated_fee: int):
        if not signed_quantity or price <= 0 or estimated_fee < 0:
            raise LedgerFault("Invalid fill for PnL accounting.")
        if execution_id in self.fees:
            raise LedgerFault("Duplicate execution cannot be applied to PnL twice.")
        remaining = signed_quantity
        while remaining and self.lots and (self.lots[0][0] > 0) != (remaining > 0):
            old_qty, old_price = self.lots[0]
            closed = min(abs(old_qty), abs(remaining))
            old_sign = 1 if old_qty > 0 else -1
            self.realized += closed * (price - old_price) * old_sign
            old_qty -= closed * old_sign
            remaining += closed * old_sign
            if old_qty:
                self.lots[0][0] = old_qty
            else:
                self.lots.popleft()
        if remaining:
            self.lots.append([remaining, price])
        self.fees[execution_id] = estimated_fee

    def commission(self, execution_id: str, fee_units: int):
        if execution_id not in self.fees:
            raise LedgerFault("Commission precedes its execution; queue it until the fill arrives.")
        self.fees[execution_id] = fee_units

    def totals(self, bid: int, ask: int) -> tuple[int, int, int, int]:
        if self.position and (bid <= 0 or ask <= 0 or bid >= ask):
            raise LedgerFault("A valid liquidation mark is required for open positions.")
        unrealized = sum(qty * ((bid if qty > 0 else ask) - price) for qty, price in self.lots)
        fees = sum(max(value, 0) for value in self.fees.values())
        rebates = sum(max(-value, 0) for value in self.fees.values())
        return self.realized, unrealized, fees, rebates


class Journal:
    def __init__(self, directory: Path, account: str, client_id: int):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_stat = directory.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_IMODE(directory_stat.st_mode) & 0o077:
            raise LedgerFault("Paper state directory must be a private, non-symlink 0700 directory.")
        if directory_stat.st_uid != os.getuid():
            raise LedgerFault("Paper state directory must belong to the current operator.")
        self.lock_fd = os.open(directory / "process.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock_fd)
            raise LedgerFault("Another paper process owns this state directory.") from None
        path = directory / "broker-state.sqlite3"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
                self.close_lock()
                raise LedgerFault("Unsafe existing broker state file.")
        self.db = sqlite3.connect(path, timeout=1)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), account TEXT NOT NULL, client_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (day TEXT PRIMARY KEY, started_ns INTEGER NOT NULL, terminal_reason TEXT);
            CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id INTEGER UNIQUE NOT NULL, day TEXT NOT NULL REFERENCES runs(day), side INTEGER NOT NULL, quantity INTEGER NOT NULL, price INTEGER NOT NULL, state TEXT NOT NULL, created_ns INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS executions (id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT UNIQUE NOT NULL, correction_group TEXT NOT NULL, order_id INTEGER NOT NULL REFERENCES orders(id), payload TEXT NOT NULL, created_ns INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS execution_group_idx ON executions(correction_group);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL, created_ns INTEGER NOT NULL);
        """)
        found = self.db.execute("SELECT account,client_id FROM identity WHERE singleton=1").fetchone()
        if found and found != (account, client_id):
            self.close()
            raise LedgerFault("State belongs to another account or client ID.")
        if not found:
            with self.db:
                self.db.execute("INSERT INTO identity VALUES (1,?,?)", (account, client_id))
        self.day: str | None = None

    def close_lock(self):
        if getattr(self, "lock_fd", None) is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def close(self):
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        self.close_lock()

    def start_once(self, day: str):
        # No restart may silently reset the current day's realized PnL or kill.
        try:
            with self.db:
                self.db.execute("INSERT INTO runs(day,started_ns) VALUES (?,?)", (day, time.time_ns()))
        except sqlite3.IntegrityError as exc:
            raise LedgerFault("This account/client state already ran today; same-day restart is unsupported and requires offline reconciliation.") from exc
        self.day = day

    def next_broker_id(self, reported_next: int) -> int:
        previous = self.db.execute("SELECT COALESCE(MAX(broker_id),0) FROM orders").fetchone()[0]
        return max(reported_next, previous + 1)

    def allocate_order(self, broker_id: int, side: int, quantity: int, price: int) -> int:
        if not self.day:
            raise LedgerFault("No active journal run.")
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO orders(broker_id,day,side,quantity,price,state,created_ns) VALUES (?,?,?,?,?,'reserved',?)",
                (broker_id, self.day, side, quantity, price, time.time_ns()),
            )
        return int(cursor.lastrowid)

    def order_state(self, order_id: int, state: str):
        with self.db:
            cursor = self.db.execute("UPDATE orders SET state=? WHERE id=?", (state, order_id))
            if cursor.rowcount != 1:
                raise LedgerFault("Unknown durable order identity.")

    def record_execution(self, execution_id: str, order_id: int, payload: dict) -> tuple[int, str]:
        if not execution_id or len(execution_id) > 256:
            raise LedgerFault("Invalid execution identity.")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        existing = self.db.execute("SELECT id,payload FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        if existing:
            if existing[1] != encoded:
                raise LedgerFault("Conflicting duplicate execution report.")
            return existing[0], "duplicate"
        group = execution_id.rsplit(".", 1)[0] if "." in execution_id else execution_id
        previous = self.db.execute("SELECT 1 FROM executions WHERE correction_group=? LIMIT 1", (group,)).fetchone()
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO executions(execution_id,correction_group,order_id,payload,created_ns) VALUES (?,?,?,?,?)",
                (execution_id, group, order_id, encoded, time.time_ns()),
            )
        return int(cursor.lastrowid), "correction" if previous else "new"

    def audit(self, kind: str, payload: dict):
        with self.db:
            self.db.execute("INSERT INTO audit(kind,payload,created_ns) VALUES (?,?,?)",
                            (kind, json.dumps(payload, allow_nan=False), time.time_ns()))

    def finish(self, reason: str):
        if self.day:
            with self.db:
                self.db.execute("UPDATE runs SET terminal_reason=? WHERE day=?", (reason, self.day))
