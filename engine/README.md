# C++20 market-data research engine

This is a real parser, order-book reconstruction engine, deterministic research
runner and tested risk reference model. It does not submit real orders. Default
replay does not infer account fills or PnL; the opt-in `--simulate` mode runs an
explicit counterfactual execution/accounting model described below. It is **not** an accepted low-latency live-trading
deployment. `std::map` and `std::unordered_map` allocate dynamically; they provide
a correctness baseline for a later independently benchmarked pool/ladder engine.

## Build and verify

The build also produces `libvector_core.so`, a single-writer C ABI over this same
C++ core for the separate paper executor. See [C_ABI.md](C_ABI.md) for its flat-only
bootstrap, genuine-live-source gate, one-time permits, PnL barrier and fault rules.

```sh
cmake -S engine -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
ctest --test-dir build --output-on-failure
python engine/tests/test_cli.py
```

For sanitizers, use a separate build directory:

```sh
cmake -S engine -B build-sanitize -DCMAKE_BUILD_TYPE=Debug -DENGINE_SANITIZERS=ON
cmake --build build-sanitize -j2
ctest --test-dir build-sanitize --output-on-failure
TRADING_REPLAY_BIN="$PWD/build-sanitize/trading-replay" python engine/tests/test_cli.py
```

## Input contract

`build/trading-replay --input /path/to/capture.itch`

Input must be an uncompressed **ITCH 5.0 length-prefixed stream**, with a two-byte
big-endian length before every message. This is not a PCAP parser, compressed
archive extractor or live multicast receiver. Supported types are `S R H Y L V W
K J h A F E C X D U P Q B I N`; unknown types fail explicitly, never skip silently.
Actual exchange protocol changes must be reviewed before enabling new types.

The parser checks exact lengths, bounded field access, endianness and supported
basic field semantics. `A/F/X/D/U/E/C` update the visible book. `P/Q` update the
public trade ledger without double-reducing displayed liquidity. `B` adjusts
trade statistics without reinstating visible orders. It never alters an account
position from a public trade message. `R/H/h/S` drive instrument/session state.

A capture must begin at the true session start (or be paired with a separately
implemented sequence-barrier snapshot) to construct a complete book. This
runner recognizes the `S/O` start marker, but a marker alone cannot prove there
were no missing messages. `book_valid` means the local aggregate checks passed
and the start marker was present. It is **not a transport completeness guarantee**.
Without the start marker, a partial capture may be parsed but is flagged invalid
and no quotes are produced. Unknown references fail rather than guessing state.

`MoldSequencer` is separately implemented and unit-tested. It validates whole
packet framing before commit, rejects a session switch, detects gaps, handles
duplicate/overlapping packets and processes heartbeat/end markers. It does not
buffer gaps or connect to retransmission/GLIMPSE services. The file replay path
does not invent network sequence numbers: `last_sequence` is a file ordinal and
`summary.transport_sequence_available` is false.

## Output and risk parameters

One JSON document is emitted on stdout only on success; stderr and exit code 2
report invalid inputs and capacity failures. The parent process must enforce its
own CPU/memory/time limits. Defaults bound files to 1 GiB, 20 million messages,
2 million visible orders and 2 million trade identities. Hard CLI maxima are
documented in `src/replay.cpp`; they are input bounds, not recommended capacity.

Flags accepted by the portal:

- `--max-order-size`: actual maximum size of each research intent.
- `--orders-per-second`: actual aggregate token bucket for research intents.
- `--soft-inventory-limit`, `--hard-inventory-limit`: in default replay these are
  recorded policy and hard-limit pricing parameters with zero research inventory;
  simulation enforces both against synthetic positions and pending exposure.
- `--daily-loss-limit-cents`: recorded but **not exercised** in default replay;
  simulation applies it to synthetic net PnL and daily high-water drawdown.
- `--tick-size`: explicit instrument tick in 1/10000 USD; default 100 is a
  configured research assumption, not a claim that all US stocks use $0.01 ticks.

All effective values and limitations are in the JSON summary. `pnl` and account
inventory are null, rather than fake zero-performance figures. "Rate admitted
intents" are not full production pretrade approvals, acknowledgements or fills.
Quote calculations use microprice, spread and inventory skew. No claim is made
that the basic research rule is profitable or a calibrated market-making model.

`checksum` combines sorted final book/trade state and quote decisions. Timing
fields are intentionally nondeterministic. The same binary, input and config
produce the same checksum; long-double pricing means cross-platform bit identity
is not promised. This is not a cryptographic checksum: the portal computes SHA256
for upload identity and integrity separately.

## Risk reference model

`RiskEngine` is a single-writer, single-instrument correctness model. It starts
disarmed; it validates health, age, price, quantity, ID, inventory endpoints,
conservative notional, available capacity and rate budget before reservation.
Pending cancel and unknown orders retain exposure. A partial fill transfers
reserved exposure to position, duplicate execution IDs do not double count, and
conflicting execution IDs fail. Legal late fills after cancel confirmation still
change position and latch kill if necessary. Daily PnL is realized + unrealized -
fees + rebates; loss and high-water drawdown latch kill. There is deliberately no
strategy-accessible reset method. The simulation runner uses this model for each
synthetic submission and execution; the default market-only runner does not.

## Automatic strategy simulation (explicit opt-in)

```sh
build/trading-replay --input /path/to/single-instrument.itch --simulate \
  --latency-ns 1000000 --cancel-latency-ns 1000000 \
  --simulation-window-ns 1000000000 \
  --fee-per-share-units 30 --rebate-per-share-units 0 \
  --min-fee-per-order-units 0 --slippage-buffer-units 0 --min-net-edge-units 0
```

The simulation implements a closed loop: valid book → inventory-skewed passive
quote → synchronous risk/reservation → delayed activation → displayed queue model
→ synthetic partial execution → FIFO accounting → mark/risk/kill → cancel/requote.
It rejects multi-instrument files rather than silently pretending a single-symbol
risk model covers an entire account. Each stock locate must be filtered correctly
upstream, preserving system events and order relationships.

The simulator never copies a public execution straight into an account. At delayed
activation, every visible share at that price is recorded ahead of the modeled
order. Later `E/C` executions against those reference IDs reduce ahead quantity,
without filling the model order. Only an execution against a different, subsequently
present reference at exactly the same resting price may fill after the ahead
quantity reaches zero. `C` with a different execution price is not eligible.
`P/Q`, hidden liquidity and unknown queue priority do not generate fills. Canceled
or replaced ahead shares are not credited, which deliberately understates fills.
The historical book remains unchanged by the counterfactual own order: this is a
zero-market-impact displayed FIFO model, not a reproduction of real matching.

Activation and cancellation use strict time comparisons, so equal-timestamp
ambiguity does not assume an early ACK. Orders remain exposed and may fill while
canceling. `--simulation-window-ns` is the **maximum quote lifetime**, implemented
as a virtual expiry timer, not a file-length cutoff. Expiry is processed even when
the next market event arrives much later. Marketable quotes at activation are
rejected by the simulated post-only rule. New quotes wait for old cancellation
confirmation on the same side. The new-order token bucket is modeled; aggregate
broker message and cancel pacing are not. No network, broker, queue-priority or
fill-probability calibration is claimed.

Accounting uses FIFO lots and checked integer money in **1/10000 USD units**:

`net = realized + unrealized - fees + rebates`

Longs mark to visible bid and shorts to visible ask. Commission for each **filled
order ID** is `max(min_fee_per_order_units, cumulative_filled_shares * fee_per_share_units)`.
Only the increase over that order's previously charged total is added on a partial
fill. For example, minimum 100 units and fee 10 units/share charge totals 100, 100,
150 for successive cumulative fills of 3, 5, 15 shares. No minimum is charged for
an unfilled or rejected order. The fee ledger is retained after cancellation and
working-slot removal; a new replacement order has its own minimum. Rebates remain
explicit per-share accounting assumptions. Regulatory fees, exchange/routing-specific
charges, commission caps, broker-specific aggregation and future liquidation fees
require a more complete fee schedule. If no
valid mark exists, `pnl_mark_valid=false`, the last computed value is not a current
valuation, new risk stops and outstanding orders are canceled. No forced, fictitious
end-of-file liquidation is performed. `inventory` and `open_orders_at_end` expose
remaining inventory/risk. Daily loss **and** maximum daily drawdown use the configured
daily-loss amount in this version; either latches kill and disables new orders.
A later fee-induced or inventory loss is not clamped to zero. Kill does not erase
existing fills or pending-cancel risk.

If a public broken trade invalidates evidence for a modeled own fill, simulation
fails explicitly: it does not invent an account bust/correction. Full correction
and hidden-liquidity models remain separate work.

Results live in `summary.simulation`; `summary.orders_sent` remains zero and the
real-account `summary.pnl` remains null. `drawdown_units` is maximum observed
drawdown. Simulation may have zero fills, negative net PnL or a kill event; no
profitability guarantee is made. A successful simulation never authorizes live
trading or implies the result is reproducible at IBKR.

## Cost-aware reservation-price quotes

Model version `displayed_fifo_conservative_v2_cost_aware` and economic policy
`conservative_one_share_v2` distinguish these results from the older fee model.
Do not pool old and new model results merely because a missing option defaults to
zero. Effective parameters, version and assumptions are included in
`summary.simulation.config` and `summary.simulation.pricing_economics`; default
market-only research also reports `summary.pricing_economics`.

Each side's distance from the **inventory-skewed reservation price** is at least:

```text
max(existing half-spread,
    fee_per_share_units + min_fee_per_order_units
    + slippage_buffer_units + min_net_edge_units)
```

The entire order minimum is allocated to the smallest possible **one-share partial
fill**, then the per-share fee is added. This deliberately exceeds
`max(minimum, variable fee)` in some cases: it is a conservative quotation policy,
not the accounting formula. Unconfirmed rebates never narrow the quote floor.
Ticks round outward and passive-price constraints can widen quotes further.
Costs can push quotes far away from the BBO or make a positive bid impossible;
the engine then has no eligible fills or suppresses invalid quotes. It never
manufactures fills or profit to compensate. `zero_fill_outcome`,
`cost_widened_decisions` and `no_valid_quote_decisions` make those outcomes explicit.

Each of the four cost components must be a nonnegative integer no greater than
1,000,000,000 money units; their checked sum is at most 4,000,000,000 units.
The three new CLI options default to zero, preserving the existing default
30-unit per-share fee / 100-unit tick fixture. Setting a large fee now changes
quotes, so an older high-fee losing fixture may correctly become a zero-fill case.
The configured `min_net_edge_units` is a desired buffer, **not a promised profit**.
Inventory skew changes the reference price, fills can be one-sided, marks can move
adversely, and the actual fee model may differ. Neither individual fills nor the
overall strategy are guaranteed to have positive net PnL.

`tests/make_simulation_fixture.py` generates an explicitly synthetic accounting
fixture solely for regression tests. Tests cover a known round trip, fee-induced
loss on an unclosed partial fill, high costs producing zero fills, incremental
minimum commissions and loss-triggered kill. It must not be presented as historical
strategy performance or user seed data.

Remaining live-trading work includes a bounded zero-allocation book/risk/OMS,
multiple-instrument account aggregate reservation, persistence and reconciliation,
execution corrections/busts/commissions, native replace semantics, venue tick and
price-band constraints, permissions/locates, network I/O and recovery, a fenced
supervisor and independently verified broker behavior. The gateway module, if
present, has its own release gates. Passing these tests does not release live
trading.

## Reproducible benchmark

The separate [ML observation and gated replay interface](ML_REPLAY.md) exports
causal L1 state and applies verified side signals to this same simulator. It
preserves default-deny participation, cancel races, risk and cost constraints;
it does not establish model profitability or authorize live trading.

```sh
python engine/tests/make_fixture.py /tmp/engine-benchmark.itch --cycles 10000
python engine/benchmark/replay_benchmark.py --binary build/trading-replay --input /tmp/engine-benchmark.itch --runs 30
```

This measures full-file processing and CLI process latency; it is not NIC-to-TX
or exchange ACK latency. The fixture is explicitly synthetic test data. For
capacity decisions use authorized real captures, hotspot/burst distributions,
larger datasets, more independent runs and transport-level load generation.
Report hardware, compiler, workload, failures and sample count with every result.
