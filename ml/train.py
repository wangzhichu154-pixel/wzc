"""Run with python -m ml.train --manifest DATASET/manifest.json --output NEW_RUN."""
from __future__ import annotations

import argparse
import json
import sys

from .features import FeatureConfig, REGULAR_CLOSE_NS, REGULAR_OPEN_NS
from .training import TrainingConfig, train_model


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train a reproducible causal Ridge baseline; no live orders")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--exporter", help="Compiled C++ ITCH exporter; defaults to build/trading-ml-replay")
    parser.add_argument("--horizon-ns", type=int, default=500_000_000)
    parser.add_argument("--max-label-delay-ns", type=int, default=250_000_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--window-events", type=int, default=32)
    parser.add_argument("--max-gap-ns", type=int, default=1_000_000_000)
    parser.add_argument("--regular-open-ns", type=int, default=REGULAR_OPEN_NS)
    parser.add_argument("--regular-close-ns", type=int, default=REGULAR_CLOSE_NS)
    parser.add_argument("--min-samples-per-session", type=int, default=64)
    parser.add_argument("--max-abs-prediction-bps", type=float, default=1000.0)
    parser.add_argument("--alphas", type=float, nargs="+", default=[0.1, 1.0, 10.0])
    args = parser.parse_args(argv)
    try:
        report = train_model(args.manifest, args.output,
                             TrainingConfig(args.horizon_ns, args.max_label_delay_ns, args.seed, tuple(args.alphas),
                                            args.min_samples_per_session, args.max_abs_prediction_bps),
                             FeatureConfig(args.window_events, args.max_gap_ns, args.regular_open_ns, args.regular_close_ns), args.exporter)
    except (ValueError, OSError, ImportError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"success": True, "model_version": report["model_version"],
                      "provenance": report["provenance"]["kind"], "split_samples": {
                          name: info["samples"] for name, info in report["split"].items()},
                      "test": report["metrics"]["test"], "eligible_for_paper": False}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
