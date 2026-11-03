# Causal observations and gated counterfactual replay

`trading-ml-replay` is a separate research executable. It shares the real ITCH
decoder, reference order book, pricing, risk reservations, order lifecycle and
displayed FIFO simulator with `trading-replay`. It does not load a model, invent
fills from prediction labels, or send broker orders. The ordinary portal replay
executable rejects the ML-specific command-line options.

## Export observations

```sh
build/trading-ml-replay --input authorized-single-symbol.itch \
  --symbol-locate 1 --export-market new-market.csv
```

The capture uses the same uncompressed two-byte length-prefixed ITCH 5.0 framing
as ordinary replay. The selected stock locate must be 1..65535. Every nonzero
locate in the capture must match it; this version rejects mixed-symbol input.
Filter a complete session upstream without losing order-reference relationships.
An unexpected second session open is rejected; separate captures/sessions must
have separate model state. Absence of the session-open event yields invalid
observations and no trading, rather than assuming a complete book.

The exact CSV schema is:

```csv
ordinal,timestamp_ns,logical_timestamp_ns,stock_locate,bid,ask,bid_qty,ask_qty,valid,tradable
```

`ordinal` is the one-based **file message ordinal**, not a proven exchange
transport sequence. `timestamp_ns` is the raw exchange nanoseconds since
midnight. `logical_timestamp_ns` is the running maximum of timestamps already
seen; it never uses future data. Regressing exchange timestamps are retained in
the raw column. Both timestamps can repeat. Prices are integer 1/10000 USD;
quantities are displayed L1 shares, **not traded volume**. All rows, including
global system-status rows, contain the configured target stock locate.

The exporter emits the initial state, changes in BBO/size/validity/tradability,
and every S/H/h status event. Invalid or halted rows are retained so feature state
can reset. A one-sided book has no valid two-sided Top and can contain zero price
fields; consumers must check both `valid` and `tradable`. State that stays invalid
need not generate another row until a status or observable BBO change. Public
prints that do not change L1 do not create a volume feature.

Rows are emitted immediately after that event's book update. Never group equal
timestamps and give the last row's values to the first row's prediction: that
would look ahead within a batch. The shared Python feature implementation must
observe each row in ordinal order, reset on invalid/halted state and long gaps,
and apply its documented sampling policy consistently offline and online.

The target must not exist, including as a symlink. A private mode-0600 temporary
file in the same directory is written and fsynced; only successful full parsing
publishes the final name atomically. Failures clean up the temporary file.
This is atomic publication, not a claim of durable directory metadata after a
host power failure. The CLI writes one JSON result to stdout, with export counts
and schema versions under `summary.ml`.

## Signals only reduce participation

```sh
build/trading-ml-replay --input authorized-single-symbol.itch \
  --symbol-locate 1 --simulate --signal-file verified-signals.csv \
  --trade-start-ns 34200000000000 --trade-end-ns 57600000000000 \
  --max-signal-age-ns 1000000000 --long-only
```

The exact signal CSV header and example shape are:

```csv
ordinal,allow_bid,allow_ask,expires_ns
12345,1,0,34201000000000
```

Values are strict unsigned decimal integers; side flags must be exactly 0 or 1.
Ordinals must be positive, unique and strictly increasing. Inputs are regular
files no larger than 128 MiB, with lines no longer than 256 bytes. Unknown
schemas, NaN, invalid integers, duplicate/decreasing ordinals and rows beyond the
capture fail the whole run without a success JSON. Header-only input is valid
and denies all orders.

The gate starts with both sides denied. A row becomes effective only at its
exact file ordinal **after book update and before the new quoting decision**.
It cannot fill or change an order on that event retroactively. The last accepted
row remains active until its exclusive expiry, a replacement signal, a market
invalid/halt/stale condition, a risk kill or the evaluation-window boundary.
Rows already expired at their application event clear permissions. A row whose
remaining lifetime exceeds `--max-signal-age-ns` is rejected (default 1 second,
maximum 1 hour). Invalid market state clears permission, so recovery requires a
new explicit prediction. Ordinary risk kill remains latched.

Gates affect side eligibility only. Pricing/cost floors, quote quantity, risk,
rate limits, reservation accounting, delayed activation, displayed queue-ahead
evidence, partial fills and per-order fees retain their authority. No predicted
future price or training label is accepted as a fill price. Permission removal
requests cancellation of working orders; configured cancellation delay and
ambiguous equal-timestamp races retain fill exposure. Known signal expiry and
window-end timers are applied even when market events skip across that time.

`--trade-start-ns` is inclusive and `--trade-end-ns` exclusive, using the causal
logical clock. Bounds must satisfy 0 <= start < end <= 24 hours. Every new order
requires both an active signal and membership in this window. The **entire
capture** is replayed after the window closes, so pending cancels, tail fills and
remaining inventory are reported. No final liquidation or automatic profitable
exit is invented. ML simulation requires an explicit signal file even for a
baseline; generate causal all-allow baseline signals with the same evaluation
window and risk/economic parameters. Export-only runs do not place orders.

`--long-only` is an optional simulation constraint (default false for compatibility
with ordinary two-sided simulations). Before each sell, actual confirmed position
minus all existing sell reservations must cover the entire order. Unfilled buys
provide no sell capacity; pending cancels keep their reservations. Insufficient
capacity rejects the full proposed sell without resizing. This can leave a
partial long inventory below quote size unclosed. The report contains
`config.long_only` and `long_only_rejections`; the run fails if its invariant ever
produces negative inventory. This does not implement securities lending or any
live account permission checks.

## Input identity is the caller's responsibility

This executable does not implement SHA256 or independently establish that a
signal was trained without leakage. Before invoking it, the controlled Python
research runner must validate the immutable manifest linking the capture hash,
re-exported observation CSV hash, feature/schema/policy versions, fitted model,
training/validation/test session split, signal hash and exact configuration.
Never accept an arbitrary user signal file as validated out-of-sample evidence.
Same ordinal in a different capture is not the same event. The engine explicitly
reports `summary.ml.capture_binding_verified_by_engine: false`; its deterministic
checksum is a regression aid, not a cryptographic identity or profitability proof.

## Metrics and interpretation

`summary.simulation.execution_metrics` derives from modeled private fills only:

- `buy_quantity`, `sell_quantity`, side notional and `turnover_units` are checked
  integer sums. Notional is price units times shares; turnover sums both sides.
- `peak_abs_inventory_shares` covers the complete replay; the `_in_window_`
  value covers fills inside the configured entry window.
- A holding episode starts at flat-to-nonflat and ends at return to flat or a
  sign change. A sign change closes one episode and starts another at that fill.
  `holding_total_ns`, `holding_max_ns`, `mean_closed_holding_ns` cover completed
  episodes; an incomplete final episode appears in `open_holding_ns`.
- `inventory_share_seconds` integrates absolute confirmed inventory between
  events over the observed overlap with the entry window.
  `time_weighted_abs_inventory_shares` divides by
  `observed_window_duration_ns`; it is null when no interval is observed.
  These analytical ratios use floating point; cash, commissions and PnL remain
  checked integer money. `full_observed_duration_ns` discloses the total extent.

`signal_gate` contains the gate policy/version, applied/expired-row counts, window
configuration, `inventory_at_window_end` (null if the capture never reaches it),
and fill count/quantity/turnover after window end. A fill exactly at the exclusive
end belongs to the tail. The position snapshot is immediately before processing
that boundary event. `open_orders_at_end`, final inventory, conservative mark
validity and unclosed holding time must accompany PnL. Existing stale-mark rules
still apply; a numeric stale mark is not evidence of realizable net profit.

The added metrics do not calibrate queue priority, market impact, hidden liquidity,
fill probability or live broker costs. Synthetic fixtures in `tests/` verify
causality, cancellation races and accounting, not model performance. Zero fills,
negative PnL and residual positions are normal possible outcomes. Live execution
and profitability remain unproven until independently validated with appropriate
authorized data and operational release gates.

## Verification

```sh
cmake -S engine -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
ctest --test-dir build --output-on-failure
```

`tests/test_ml_cli.py` runs the actual compiled executable. It covers atomic
export, invalid/halted rows, same-timestamp causality, raw-clock regression,
default deny, future signal isolation, gate cancellation/expiry, tail fill races,
real FIFO PnL/fees, deterministic checksums, long-only inventory and hard risk/cost
constraints. Python is needed for CLI integration tests; C++ core/C ABI tests
remain independent of Python and any broker SDK.

## Account valuation evidence (rc.4)

`summary.simulation.valuation` separates reported account equity from permission
to quote. The existing top-level `pnl_mark_valid`, realized/unrealized/net fields,
strategy behavior and synchronous risk checks retain their original semantics.
In particular, this addition never makes a closed, halted or stale market
tradable and never releases an unconfirmed order reservation.

The added object has this exact schema:

```json
{
  "schema_version": 1,
  "basis": "reconciled_flat_ledger",
  "quote_mark_valid": false,
  "net_pnl_valid": true,
  "unrealized_pnl_units": 0,
  "net_pnl_units": 0,
  "drawdown_complete": true,
  "max_drawdown_units": 0
}
```

All monetary values use 1/10000 USD, as elsewhere in the engine. The example is
the actual shape of a zero-fill, zero-inventory, no-working-order account after
market close, not evidence that a strategy can generate profit.

| Basis | Required evidence | Monetary outputs |
|---|---|---|
| `fresh_bbo` | Existing `pnl_mark_valid` is true | Same marked unrealized/net PnL as the existing accounting ledger; pending orders remain explicitly reported |
| `reconciled_flat_ledger` | Fresh BBO is unavailable; actual position is zero, FIFO lots empty, working/OMS order counts and both worst-case reservation endpoints zero, and cash before fees equals realized PnL | Unrealized is exactly zero; net is `realized - fees + rebates`, independent of a closing quote |
| `unavailable` | Neither condition is met | Unrealized/net are `null`; no stale value is substituted |

The new drawdown tracker starts from zero account PnL and uses actual modeled
fill accounting. It is separate from the existing risk engine's high watermark.
Any nonzero inventory observed without a fresh mark permanently invalidates the
drawdown path for this capture. Before processing each later event, it also
checks whether previously held inventory crossed the BBO freshness deadline;
an incoming fresh quote or flattening execution cannot hide that missing
interval. Invalidity is not reset by a later quote or flat inventory.

When final net valuation is unavailable, `drawdown_complete` is false and
`max_drawdown_units` is `null`. A reconciled final account can have valid net PnL
but incomplete drawdown: in that case only net PnL is published, and drawdown
remains null. Flat warmup/closed periods do not themselves invalidate an otherwise
complete account path. This is event-observed drawdown under the declared stale
quote rule; it does not claim knowledge of price excursions between feed events.

The C++ tests execute real protocol fixtures covering a closed zero-fill account,
an actual simulated FIFO round trip followed by terminal cancels, stale nonzero
inventory, a cancel still pending at close, a halt and subsequent recovery, and a
time gap followed by a newly fresh book. No test bypasses the simulator to insert
private fills or to reset the risk engine.
