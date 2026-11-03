"""Read-only projection of a separately supervised paper execution process."""
import json
from pathlib import Path
import time

from django.conf import settings

STATES = {"read_only", "awaiting_feed", "running", "halted", "reconcile_required", "disconnected"}


def get_paper_status():
    unavailable = {"available": False, "state": "not_configured", "live_enabled": False}
    if not settings.PAPER_STATUS_PATH:
        return unavailable
    path = Path(settings.PAPER_STATUS_PATH)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return {**unavailable, "state": "unavailable"}
        with path.open("rb") as source:
            payload = source.read(65537)
        if len(payload) > 65536:
            return {**unavailable, "state": "invalid"}
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("kind") != "ibkr-paper":
            return {**unavailable, "state": "invalid"}
        timestamp = data.get("updated_at_ns")
        if type(timestamp) is not int or data.get("state") not in STATES:
            return {**unavailable, "state": "invalid"}
        age = (time.time_ns() - timestamp) / 1e9
        if age < -1 or age > settings.PAPER_STATUS_MAX_AGE_SECONDS:
            return {**unavailable, "state": "stale"}
        clean = {"available": True, "state": data["state"], "live_enabled": False, "age_seconds": round(age, 3)}
        for key in ("connected", "armed", "pnl_mark_valid", "kill_latched"):
            if type(data.get(key)) is not bool:
                return {**unavailable, "state": "invalid"}
            clean[key] = data[key]
        for key in ("inventory", "open_orders"):
            if type(data.get(key)) is not int:
                return {**unavailable, "state": "invalid"}
            clean[key] = data[key]
        pnl = data.get("net_pnl_units")
        if pnl is not None and type(pnl) is not int:
            return {**unavailable, "state": "invalid"}
        clean["net_pnl_units"] = pnl if clean["pnl_mark_valid"] else None
        # Explicit allowlist; account IDs, diagnostics and unknown fields omitted.
        return clean
    except (OSError, ValueError, TypeError, RecursionError):
        return {**unavailable, "state": "unavailable"}
