"""Shared pre-training holdout reservations for pipeline and periodic retraining.

All public mutations require the caller to hold Registry._lock(). Failed jobs
retain their reservations. This ledger protects a local operator-owned store;
it is not a distributed lock or an attestation of data licensing.
"""
from __future__ import annotations

from datetime import date
import re
import time

from .registry import HASH_RE, RegistryError, _present, _write, read_json

LEDGER_PATH = "research/reservations.json"
MAX_REQUESTS = 1000
MAX_KEYS = 10000


class ReservationConflict(RegistryError):
    pass


def _session(value):
    if (not isinstance(value, dict) or set(value) != {"capture_id", "session_date", "itch_sha256"}
            or not isinstance(value["capture_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", value["capture_id"])
            or not isinstance(value["session_date"], str)
            or not isinstance(value["itch_sha256"], str) or not HASH_RE.fullmatch(value["itch_sha256"])):
        raise RegistryError("Invalid reserved test session")
    try:
        if date.fromisoformat(value["session_date"]).isoformat() != value["session_date"]:
            raise ValueError("Noncanonical date")
    except ValueError as exc:
        raise RegistryError("Invalid reserved session date") from exc
    return value


def _keys(symbol, sessions):
    return sorted({"sha256:" + item["itch_sha256"] for item in sessions}
                  | {"session:" + symbol + ":" + item["session_date"] for item in sessions})


def validate_ledger(value):
    """Validate retained reservations, including during backup and restore."""
    if (not isinstance(value, dict) or set(value) != {"schema_version", "requests"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["requests"], dict) or len(value["requests"]) > MAX_REQUESTS):
        raise RegistryError("Invalid research reservation ledger")
    used = set()
    fields = {"owner", "request_sha256", "dataset_sha256", "manifest_sha256", "symbol",
              "test_sessions", "holdout_keys", "reserved_at_ns"}
    for identity, entry in value["requests"].items():
        if (not isinstance(entry, dict) or set(entry) != fields or entry["owner"] not in ("pipeline", "retrain")
                or any(not isinstance(entry[key], str) or not HASH_RE.fullmatch(entry[key])
                       for key in ("request_sha256", "dataset_sha256", "manifest_sha256"))
                or identity != entry["owner"] + ":" + entry["request_sha256"]
                or not isinstance(entry["symbol"], str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", entry["symbol"])
                or type(entry["reserved_at_ns"]) is not int or entry["reserved_at_ns"] <= 0
                or not isinstance(entry["test_sessions"], list) or not 1 <= len(entry["test_sessions"]) <= 512):
            raise RegistryError("Invalid research reservation identity")
        sessions = [_session(item) for item in entry["test_sessions"]]
        dates = [item["session_date"] for item in sessions]
        if (dates != sorted(set(dates)) or len({item["capture_id"] for item in sessions}) != len(sessions)
                or len({item["itch_sha256"] for item in sessions}) != len(sessions)):
            raise RegistryError("Reserved sessions must be unique and chronological")
        keys = _keys(entry["symbol"], sessions)
        if entry["holdout_keys"] != keys or used.intersection(keys):
            raise RegistryError("Research reservations contain duplicate or inconsistent holdouts")
        used.update(keys)
    if len(used) > MAX_KEYS:
        raise RegistryError("Research reservation capacity requires operator maintenance")
    return value


def read_ledger(registry):
    path = registry.root / LEDGER_PATH
    return validate_ledger(read_json(path) if _present(path) else {"schema_version": 1, "requests": {}})


def _validate_consumed_keys(values):
    for key, identity in values.items():
        if not isinstance(identity, str) or not HASH_RE.fullmatch(identity):
            raise RegistryError("Invalid consumed holdout experiment identity")
        if key.startswith("sha256:") and HASH_RE.fullmatch(key[7:]):
            continue
        match = re.fullmatch(r"session:([A-Z][A-Z0-9.]{0,7}):(\d{4}-\d{2}-\d{2})", key)
        if match is None:
            raise RegistryError("Invalid consumed holdout source or session key")
        try:
            if date.fromisoformat(match[2]).isoformat() != match[2]:
                raise ValueError("Noncanonical date")
        except ValueError as exc:
            raise RegistryError("Invalid consumed holdout session date") from exc


def known_holdouts(registry, symbol):
    """Even a rejected or unevaluated registered candidate consumed its test."""
    versions = list((registry.root / "versions").iterdir())
    if len(versions) > MAX_REQUESTS:
        raise RegistryError("Model history requires bounded operator maintenance")
    keys, dates = set(), []
    for directory in versions:
        if directory.name.startswith(".candidate-"):
            raise RegistryError("Interrupted registration requires operator inspection")
        known = registry._manifest(directory.name)
        artifact = registry._check_model(directory.name, known)
        sessions = artifact.training_metadata.get("split", {}).get("test", {}).get("sessions")
        if not isinstance(sessions, list) or not sessions:
            raise RegistryError("Registered candidate lacks an immutable test partition")
        captures = {capture["capture_id"]: capture for capture in known["captures"]}
        for session in sessions:
            capture = captures.get(session.get("capture_id"))
            if capture is None or session.get("session_date") != capture["session_date"]:
                raise RegistryError("Registered candidate test identity mismatch")
            keys.add("sha256:" + capture["itch_sha256"])
            keys.add("session:" + known["symbol"] + ":" + capture["session_date"])
            if known["symbol"] == symbol:
                dates.append(capture["session_date"])
    return keys, max(dates) if dates else None


def _entry(data, owner, request_sha256, now_ns):
    if owner not in ("pipeline", "retrain") or not isinstance(request_sha256, str) or not HASH_RE.fullmatch(request_sha256):
        raise RegistryError("Invalid reservation request identity")
    if len(data.captures) < 10 or len({item.symbol for item in data.captures}) != 1:
        raise RegistryError("At least ten single-symbol sessions are required before reserving a holdout")
    sessions = [{"capture_id": item.capture_id, "session_date": item.session_date, "itch_sha256": item.itch_sha256}
                for item in data.captures[int(len(data.captures) * 0.8):]]
    symbol = data.captures[0].symbol
    return {"owner": owner, "request_sha256": request_sha256, "dataset_sha256": data.dataset_sha256,
            "manifest_sha256": data.manifest_sha256, "symbol": symbol, "test_sessions": sessions,
            "holdout_keys": _keys(symbol, sessions), "reserved_at_ns": now_ns}


def check_available(registry, data):
    """Read-only freshness check under the registry writer lock."""
    wanted = _entry(data, "pipeline", "0" * 64, 1)
    keys = set(wanted["holdout_keys"])
    known, latest = known_holdouts(registry, wanted["symbol"])
    path = registry.root / "holdouts.json"
    consumed = read_json(path) if _present(path) else {"schema_version": 1, "entries": {}}
    if (type(consumed.get("schema_version")) is not int or consumed.get("schema_version") != 1 or not isinstance(consumed.get("entries"), dict)
            or len(consumed["entries"]) > MAX_KEYS):
        raise RegistryError("Invalid registry holdout ledger")
    _validate_consumed_keys(consumed["entries"])
    known.update(consumed["entries"])
    path = registry.root / "retrain/attempts.json"
    attempts = read_json(path) if _present(path) else {"schema_version": 2, "datasets": {}, "holdout_keys": {}}
    if attempts.get("schema_version") == 1:
        raise RegistryError("Legacy retraining ledger requires an explicit --migrate-attempts upgrade")
    if (type(attempts.get("schema_version")) is not int or attempts.get("schema_version") != 2 or not isinstance(attempts.get("datasets"), dict)
            or not isinstance(attempts.get("holdout_keys"), dict) or len(attempts["datasets"]) > MAX_REQUESTS
            or len(attempts["holdout_keys"]) > MAX_KEYS):
        raise RegistryError("Invalid retraining holdout ledger")
    if any(not HASH_RE.fullmatch(key) or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{24}", value)
           for key, value in attempts["datasets"].items()):
        raise RegistryError("Invalid retraining dataset identity")
    _validate_consumed_keys(attempts["holdout_keys"])
    if any(identity not in attempts["datasets"] for identity in attempts["holdout_keys"].values()):
        raise RegistryError("Retraining holdout reservation lacks its attempted dataset")
    known.update(attempts["holdout_keys"])
    if data.dataset_sha256 in attempts["datasets"]:
        raise ReservationConflict("Dataset was already attempted; obtain new independent test sessions")
    for entry in read_ledger(registry)["requests"].values():
        known.update(entry["holdout_keys"])
    prefix = "session:" + wanted["symbol"] + ":"
    for key in known:
        if key.startswith(prefix):
            day = key[len(prefix):]
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError("Noncanonical date")
            except ValueError as exc:
                raise RegistryError("Invalid consumed session identity") from exc
            latest = max(latest, day) if latest else day
    if keys.intersection(known) or latest is not None and wanted["test_sessions"][0]["session_date"] <= latest:
        raise ReservationConflict("Test source or session was already reserved or consumed; obtain later independent sessions")


def reserve(registry, data, *, owner, request_sha256, allow_existing=False, now_ns=None):
    """Atomically reserve before spawning training; caller holds Registry._lock()."""
    now = time.time_ns() if now_ns is None else now_ns
    wanted = _entry(data, owner, request_sha256, now)
    identity = owner + ":" + request_sha256
    ledger = read_ledger(registry)
    existing = ledger["requests"].get(identity)
    if existing is not None:
        if not allow_existing or any(existing[key] != value for key, value in wanted.items() if key != "reserved_at_ns"):
            raise ReservationConflict("Reservation belongs to another invocation or differs from the frozen request")
        return existing
    check_available(registry, data)
    ledger["requests"][identity] = wanted
    validate_ledger(ledger)
    _write(registry.root / LEDGER_PATH, ledger, replace=True)
    return wanted
