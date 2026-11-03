"""Bound numerical forecasts to real C++ FIFO replay, with fixed cost stress.

Simulation is counterfactual, not an IBKR paper account. Test reports are final
evaluation evidence, not a parameter-search objective. No real orders are sent.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import fcntl
import json
from pathlib import Path
import subprocess
import time

from .data import iter_market_rows, load_manifest, sha256_file, verify_market_exports
from .features import FeatureState
from .model import LinearModel, canonical_bytes, write_json
from .runtime import PolicyConfig, TradingPolicy
from .valuation import aggregate_sessions, evaluation_contract, research_source_fingerprints, validate_valuation


def _hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {name: None for name in ("p50_ns", "p90_ns", "p99_ns", "p999_ns", "max_ns")}
    return {name: ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]
            for name, q in (("p50_ns", .5), ("p90_ns", .9), ("p99_ns", .99), ("p999_ns", .999), ("max_ns", 1))}


def _signals(capture, model, config, directory):
    state, policy = FeatureState(model.feature_config), TradingPolicy(model, config)
    paths = {name: directory / f"{capture.capture_id}-{name}.csv" for name in ("model", "baseline", "wait")}
    streams = {name: path.open("x") for name, path in paths.items()}
    reasons, latency, count = Counter(), [], 0
    first, last = None, None
    previous_market = None
    try:
        for stream in streams.values():
            stream.write("ordinal,allow_bid,allow_ask,expires_ns\n")
        with (directory / f"{capture.capture_id}-signals.jsonl").open("x") as structured:
            for row in iter_market_rows(capture):
                started = time.perf_counter_ns()
                previous_resets = state.resets
                sample = state.update(row)
                signature = tuple(row[name] for name in ("bid", "ask", "bid_qty", "ask_qty", "valid", "tradable"))
                # Only unchanged traffic retains an existing lifetime. A changed
                # book with no newly eligible feature explicitly cancels permission,
                # matching the broker even for multiple changes at one timestamp.
                unchanged = signature == previous_market
                previous_market = signature
                if sample is None and row["valid"] and row["tradable"] and unchanged and state.resets == previous_resets:
                    continue
                now = row["logical_timestamp_ns"]
                signal = policy.research_participation(sample, symbol=capture.symbol, now_ns=now, market=row)
                latency.append(time.perf_counter_ns() - started)
                if sample is not None:
                    first = now if first is None else first
                    last = now
                reasons.update(signal["reason_codes"] or ["ALLOW_DIRECTION"])
                structured.write(json.dumps(signal, separators=(",", ":"), allow_nan=False) + "\n")
                seq, expiry = row["sequence"], signal["expires_ns"]
                streams["model"].write(f"{seq},{int(signal['allow_bid'])},{int(signal['allow_ask'])},{expiry}\n")
                valid = sample is not None
                streams["baseline"].write(f"{seq},{int(valid)},{int(valid)},{now + config.signal_ttl_ns if valid else now}\n")
                streams["wait"].write(f"{seq},0,0,{now}\n")
                count += 1
    finally:
        for stream in streams.values():
            stream.close()
    if first is None or last is None or first >= last:
        raise ValueError("No usable chronological decision window")
    identity = {"capture_id": capture.capture_id, "itch_sha256": capture.itch_sha256,
                "market_csv_sha256": capture.market_csv_sha256, "model_version": model.model_version,
                "policy_sha256": config.sha256, "signals": {key: sha256_file(path) for key, path in paths.items()}}
    write_json(directory / f"{capture.capture_id}-binding.json", identity)
    return paths, first, last + 1, count, reasons, latency


def _run(binary, capture, signal_path, start, end, config, *, multiplier=1, latency_multiplier=1):
    before = sha256_file(signal_path)
    contract = evaluation_contract()
    arguments = [str(binary), "--input", str(capture.itch_path), "--symbol-locate", str(capture.stock_locate),
                 "--simulate", "--signal-file", str(signal_path), "--trade-start-ns", str(start), "--trade-end-ns", str(end),
                 "--max-signal-age-ns", str(config.signal_ttl_ns), "--max-order-size", str(contract["maximum_order_shares"]),
                 "--soft-inventory-limit", str(max(1, config.max_target_position // 2)),
                 "--hard-inventory-limit", str(config.max_target_position), "--orders-per-second", str(contract["new_order_rate"]),
                 "--daily-loss-limit-cents", str(contract["daily_loss_limit_cents"]),
                 "--latency-ns", str(contract["latency_ns"] * latency_multiplier),
                 "--cancel-latency-ns", str(contract["cancel_latency_ns"] * latency_multiplier),
                 "--simulation-window-ns", str(contract["quote_lifetime_ns"]),
                 "--tick-size", str(contract["tick_size"]), "--min-net-edge-units", str(contract["min_net_edge_units"]),
                 "--fee-per-share-units", str(config.fee_per_share_units * multiplier),
                 "--min-fee-per-order-units", str(config.min_fee_per_order_units * multiplier),
                 "--slippage-buffer-units", str(config.slippage_buffer_units * multiplier), "--rebate-per-share-units", "0"]
    if not config.allow_short:
        arguments.append("--long-only")
    result = subprocess.run(arguments, capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError(f"C++ simulator rejected {capture.capture_id}: {result.stderr[-2000:].decode(errors='replace')}")
    if before != sha256_file(signal_path) or sha256_file(capture.itch_path) != capture.itch_sha256:
        raise ValueError("Simulation inputs changed during execution")
    if len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("Unbounded simulator report")
    payload = json.loads(result.stdout)
    simulation = payload["simulation"] if "simulation" in payload else payload["summary"]["simulation"]
    validate_valuation(simulation)
    if simulation["config"]["participation_gate_required"] is not True:
        raise ValueError("Engine did not enforce required AI gate")
    if not config.allow_short and simulation["config"].get("long_only") is not True:
        raise ValueError("Engine did not enforce independent long-only risk")
    return payload, simulation


def _aggregate(sessions):
    return aggregate_sessions(sessions)

def backtest(manifest, training_run, output, binary, admission_path, split="test"):
    dataset = load_manifest(manifest)
    binary = Path(binary).resolve(strict=True)
    verify_market_exports(dataset, binary)
    training_run, output = Path(training_run), Path(output)
    model_path = training_run / "model.json"
    model = LinearModel.load(model_path)
    training = json.loads((training_run / "report.json").read_text())
    admission = json.loads(Path(admission_path).read_text())
    from .registry import validate_admission
    validate_admission(admission)
    config = PolicyConfig(**admission["policy"], clock="exchange_logical")
    if config.max_target_position < 2:
        raise ValueError("Simulation requires hard inventory >= 2 for a strict soft threshold")
    expected_splits = {name: [session["capture_id"] for session in value["sessions"]]
                       for name, value in model.training_metadata["split"].items()}
    if (training["model_version"] != model.model_version
            or training.get("model_sha256") != sha256_file(model_path)
            or model.training_metadata["dataset_sha256"] != dataset.dataset_sha256
            or training.get("dataset_sha256") != dataset.dataset_sha256
            or training.get("data_manifest_sha256") != dataset.manifest_sha256
            or training.get("split") != model.training_metadata["split"]
            or training.get("splits") != expected_splits):
        raise ValueError("Model/report/data identity mismatch")
    wanted = set(training["splits"][split])
    captures = [capture for capture in dataset.captures if capture.capture_id in wanted]
    if not captures or len(captures) != len(wanted):
        raise ValueError("Unknown split captures")
    output.mkdir(parents=True, exist_ok=False)
    # Commit scenarios before observing a result; never choose the best cost case.
    plan = {**evaluation_contract(), "split": split, "model_sha256": sha256_file(model_path),
            "policy_sha256": config.sha256, "admission_sha256": _hash(admission),
            "engine_sha256": sha256_file(binary),
            "python_source_sha256": research_source_fingerprints(),
            "dataset_sha256": dataset.dataset_sha256}
    write_json(output / "evaluation-plan.json", plan)
    if split == "test":
        ledger = dataset.path.parent / ".simulation-holdouts"
        ledger.mkdir(mode=0o700, exist_ok=True)
        if ledger.is_symlink():
            raise ValueError("Unsafe evaluation ledger")
        holdout_key = _hash(sorted(c.itch_sha256 for c in captures))
        record = {"holdout_sha256": holdout_key, "plan_sha256": _hash(plan)}
        lock = ledger / ".lock"
        if lock.is_symlink():
            raise ValueError("Unsafe evaluation lock")
        with lock.open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            keys = sorted({key for c in captures for key in (c.itch_sha256, _hash([c.symbol, c.session_date]))})
            paths = [ledger / f"{key}.json" for key in keys]
            for path in paths:
                if path.is_symlink() or (path.exists() and json.loads(path.read_text()) != record):
                    raise ValueError("Test sessions already evaluated with another policy/model; obtain a fresh holdout")
            for path in paths:
                if not path.exists():
                    write_json(path, record)
    results = {name: [] for name in plan["scenarios"]}
    all_latency, all_reasons = [], Counter()
    for capture in captures:
        signals, start, end, count, reasons, latency = _signals(capture, model, config, output)
        all_latency.extend(latency)
        all_reasons.update(reasons)
        scenarios = (("model", "model", 1, 1), ("cost_aware_two_sided", "baseline", 1, 1),
                     ("wait", "wait", 1, 1), ("double_cost", "model", 2, 1), ("fivefold_latency", "model", 1, 5))
        for name, source, multiplier, latency_multiplier in scenarios:
            payload, simulation = _run(binary, capture, signals[source], start, end, config,
                                       multiplier=multiplier, latency_multiplier=latency_multiplier)
            write_json(output / f"{capture.capture_id}-{name}-engine.json", payload)
            results[name].append({"capture_id": capture.capture_id, "session_date": capture.session_date,
                                  "events": count, "start_ns": start, "end_ns": end, "simulation": simulation,
                                  "engine_report": payload, "engine_report_sha256": _hash(payload)})
    aggregate = {name: _aggregate(rows) for name, rows in results.items()}
    train_end = max(c.session_date for c in dataset.captures if c.capture_id in training["splits"]["train"])
    val_end = max(c.session_date for c in dataset.captures if c.capture_id in training["splits"]["validation"])
    input_identity = [{"capture_id": c.capture_id, "session_date": c.session_date,
                       "itch_sha256": c.itch_sha256, "market_csv_sha256": c.market_csv_sha256} for c in captures]
    report = {"schema_version": 2, "report_type": "simulation_validation", "split": split,
              "model_version": model.model_version, "model_sha256": sha256_file(model_path),
              "policy_sha256": config.sha256, "admission_sha256": _hash(admission),
              "input_sha256": _hash({c.capture_id: c.itch_sha256 for c in captures}), "inputs": input_identity,
              "capture_ids": [c.capture_id for c in captures],
              "input_hashes": {c.capture_id: c.itch_sha256 for c in captures},
              "engine_sha256": sha256_file(binary), "provenance_kind": dataset.provenance["kind"],
              "python_source_sha256": plan["python_source_sha256"],
              "evaluation_plan": plan, "evaluation_plan_sha256": _hash(plan),
              "chronological_holdout": captures[0].session_date > (val_end if split == "test" else train_end),
              "time_boundaries": {"training_end_date": val_end, "fitting_end_date": train_end, "validation_end_date": val_end,
                                  "simulation_start_date": captures[0].session_date, "simulation_end_date": captures[-1].session_date},
              "metrics": aggregate["model"], "simulation_summary": aggregate["model"],
              "baselines": {key: aggregate[key] for key in ("cost_aware_two_sided", "wait")},
              "baseline_by_session": {key: results[key] for key in ("cost_aware_two_sided", "wait")},
              "cost_stress": [{"multiplier": 2, "metrics": aggregate["double_cost"], "by_session": results["double_cost"],
                               "fee_per_share_units": config.fee_per_share_units * 2,
                               "min_fee_per_order_units": config.min_fee_per_order_units * 2,
                               "slippage_buffer_units": config.slippage_buffer_units * 2}],
              "latency_stress": {"multiplier": 5, "metrics": aggregate["fivefold_latency"], "by_session": results["fivefold_latency"]},
              "by_session": results["model"], "signal_reason_counts": dict(all_reasons),
              "measured_internal_python_feature_and_policy": {**_percentiles(all_latency), "samples": len(all_latency),
                    "scope": "Feature calculation + policy in offline Python process; excludes NIC, broker, ACK and subprocess overhead"},
              "broker_latency": {"status": "unmeasured", "orders_sent": 0},
              "limitations": ["Synthetic fixtures validate software only; no real-market profitability claim",
                  "Displayed FIFO with delayed activation/cancellation; touching quote is not a fill",
                  "Slippage buffer widens quoted prices; this is not calibrated realized slippage or market impact",
                  "Sessions independently start flat; residual exposure is reported, not forcibly liquidated",
                  "Regime results are per-session; no externally verified bull/bear regime data available",
                  "No paper/live broker execution in this report"], "execution_authorized": False}
    write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--training-run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--admission", required=True, type=Path)
    parser.add_argument("--binary", type=Path, default=Path("build/trading-ml-replay"))
    parser.add_argument("--split", choices=("validation", "test"), default="test")
    args = parser.parse_args()
    try:
        report = backtest(args.manifest, args.training_run, args.output, args.binary, args.admission, args.split)
        print(json.dumps({"model_version": report["model_version"], "metrics": report["metrics"], "output": str(args.output)}))
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"Backtest failed: {exc}\n")


if __name__ == "__main__":
    main()
