# C++ core C ABI for a separate paper executor

Build artifact: `build/libvector_core.so`. Public C-compatible header:
`engine/include/trading/vector_core.h`. This ABI exposes the **same** ITCH parser,
Book, Pricing and RiskEngine used by the tests, rather than a second Python risk
implementation. It uses fixed-width structs and opaque handles, not JSON or RPC.

It is a correctness/reference integration boundary. A successful library build or
unit test does not constitute IBKR SDK compatibility, live-feed authorization,
paper-account connectivity, complete reconciliation or production acceptance.

## ABI and ownership

- Set `abi_version=VC_ABI_VERSION` and `struct_size=sizeof(vc_config)`.
- Zero-initialize structs and reserved fields. `symbol` is at most 8 printable
  non-space characters with a trailing NUL, and must match the daily stock locate.
- Create, use and destroy a handle on **one owner thread**. Other-thread calls
  return `VC_INVALID_ARGUMENT`; SDK callbacks must be queued to the owner.
- Price and PnL are signed integer **1/10000 USD**; quantity is shares. Side is
  `0=Buy`, `1=Sell`. Clock values are caller-supplied monotonic nanoseconds.
- No C++ exception crosses the exported boundary. Protocol/invariant failures
  latch kill and return an error. `vc_last_error` supplies bounded diagnostic text.
- Caller owns input/output storage. Never use a destroyed handle or destroy it
  while another caller could access it; the ABI cannot validate arbitrary pointers.
- The library deliberately has no kill-reset API. In-memory kill is not durable:
  the executor/supervisor must persist the event and never bypass it by restart.

## Startup order

1. Create a configured single-instrument handle. It starts disarmed. Immediately
   call the additive ABI-1 function
   `vc_set_quote_economics(handle, fee_per_share, min_fee_per_order, slippage_buffer, min_net_edge)`.
   The existing ABI-1 structs are unchanged, but a binding lacking this required
   export must fail closed. Until configured, the library provides market state
   but no quote intent and refuses arming. Each component is an integer in
   `0..1e9` units, with a checked total at most `4e9`. Configuration is atomic;
   invalid arguments return `VC_INVALID_ARGUMENT` without partially modifying
   earlier settings. It is allowed only before the first successful arm and with
   no risk reservations. The first arm permanently freezes it for this handle,
   even after a later kill or all orders becoming terminal.
2. Connect an independently verified **paper** account and receive all broker
   open-order, position and execution-reconciliation end markers. Existing orders
   or nonzero position are unsupported at bootstrap and must be rejected, not
   silently ignored. Daily realized PnL and fees also require a correct baseline;
   flat position alone does not prove daily PnL is zero.
3. Consume an actual live source with an authenticated/authorized source contract.
   `vc_process_itch` accepts one raw ITCH message **without** its two-byte file
   prefix. The feed adapter owns MoldUDP64 sequencing, redundant feed handling and
   sequence-1/session-start completeness. It must validate sequencing before
   forwarding messages. A gap, disconnect or untrusted source is a kill condition.
4. Set health flags only after observed readiness. A recorded file does **not**
   become live because it is replayed quickly. `live_source=false` prevents arming;
   the ABI cannot cryptographically prove a caller's source declaration. The
   external executor must enforce this distinction, and must not accept a history
   path as live execution input.
5. A real `S/O` session start, continuous trading state, configured symbol identity
   and a fresh valid BBO must all be seen by the C++ book. No arbitrary mid-session
   snapshot bootstrap is supported in this version.
6. Supply reconciled PnL via `vc_update_pnl`, then call `vc_arm_flat` with verified
   paper identity, completed reconciliation, zero broker open orders and zero
   broker position. All conditions are independently checked against local state.

Freshness uses the receive timestamp of the latest **actual valid BBO change**,
not the timestamp of any later metadata message. Paused/closed markets, missing
depth and an expired BBO suppress quotes. When already armed, invalid markets or
expired BBO observations latch a feed fault and retain open-order exposure.
RX timestamps may precede a newer control-loop clock sample because of queueing;
only regression within the RX stream, or regression of caller control time, is
treated as a clock fault.

## Submission and lifecycle

```text
get_quote(now) → plan order
  → check_and_reserve(id,side,qty,price,now,deadline)
  → receive exact vc_permit
  → send_boundary(permit,now)
  → exactly one SDK submission attempt
  → ack / fill / reject / cancel / unknown callbacks on owner thread
```

`check_and_reserve` validates current health, mark freshness, tick, passive price,
inventory endpoints, notional, capacity, duplicate identity and new-order pacing.
It then reserves exposure. `send_boundary` rechecks safety and consumes a **one-time
permit**, including its exact stored deadline and epoch. It rejects repeated use,
forged deadline extensions, expired permits and a change to an aggressive price.
Returning from this function is not a broker acknowledgement. An SDK exception
after it is `outcome_unknown` unless non-submission can be independently proven.

`vc_definitely_not_sent` and `vc_reject` release exposure only for a known unsent
or definitively rejected order. A failed send-boundary check leaves the reservation
until the owner confirms it was not sent. Pending cancellation and unknown outcomes
continue to reserve exposure. A confirmed cancellation releases only the remaining
reservation; a legitimate late execution can still change position afterward.

`vc_fill` accepts a stable execution identity generated by a collision-free mapping
of the broker's complete execution ID. It returns `VC_OK` for a new fill and
`VC_DUPLICATE` for an identical replay. A conflicting identity is a fault. The
function updates position and reserved quantity, **not** realized/cash accounting.
Every new fill blocks further submissions until the caller updates PnL. The
executor must maintain a checked FIFO/cash ledger, conservative valid BBO marks,
estimated fees and later exact commission adjustments, then call `vc_update_pnl`
with **totals**, not fee deltas. Duplicate reports must not change accounting twice.

Any unsupported execution correction calls `vc_execution_correction`, latching
kill and requiring complete external reconciliation. It must not be treated as a
duplicate ordinary fill. Unknown orders, outcome ambiguity, broker disconnect and
loss/drawdown also latch. Continue processing valid late fills and cancellation
reports after a kill; exiting immediately can leave live paper orders working.

`vc_cancel_request` remains available after kill. The external gateway still owns
cancel pacing, actual submission, acknowledgement tracking and out-of-band
protection. The C++ library cannot cancel a broker order itself.

The economic setter affects **quote construction**, not the actual accounting
ledger supplied by the executor. Its per-side floor is
`fee_per_share + min_fee_per_order + slippage_buffer + min_net_edge` around the
inventory-skewed reservation price, or the existing half-spread if larger. It
deliberately allocates the full minimum to a possible one-share partial fill and
then adds per-share fees; it does not assume a full-size fill amortizes the minimum.
The resulting floor is more conservative than `max(minimum, variable fee)`, which
the accounting ledger uses. Rebates do not reduce it. This may cause distant
quotes or no valid quote/fills, especially for small paper orders with minimum
commissions. It does not guarantee per-fill or total net profits, and it is not
an edge guarantee relative to the unskewed market midprice. The executor still
reports actual/estimated cumulative fees and exact later adjustments through
`vc_update_pnl`; configuring quote costs does not apply those charges twice.

## Outputs and errors

`vc_get_quote` can return `VC_OK` with `quote_available=0`. It always supplies the
last BBO and explicit freshness/trading-state flags when available, including
after kill for conservative marking. It does not authorize sending by itself.
`vc_snapshot` includes epoch, kill/armed flags, reconciliation state, current
position, both reserved directions, open-order count and the most recent reject.

`VC_RECONCILIATION_REQUIRED` is a safety state, not a request to discard callbacks.
The owner should disable new orders, attempt cancellation through the real gateway,
continue consuming reports and persist an incident for the supervisor. An
unconfirmed cancellation must be reported as unconfirmed.

## Remaining live execution requirements

This version remains single-instrument and uses the reference allocating book.
The external executor must enforce account/contract/routing identity, current
exchange and short-sale rules, locate/borrow constraints, broker aggregate and
cancel pacing, current SDK semantics, execution-ID provenance, durable journals,
clock/source integrity, process fencing and independent emergency cancellation.
No cross-symbol global risk, warm-position bootstrap, commission reconciliation
proof, native exchange ACK guarantee or HFT latency SLA is implied.
