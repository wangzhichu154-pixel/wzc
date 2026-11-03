# Research application contract

This application is the persistent research control plane. It cannot place live
broker orders. `PlatformSafetyState` pauses research task intake/execution and
must never be described as exchange-side cancellation or a live-trading kill.

## Workflow

1. A verified active user uploads a private `.itch` file.
2. Every 2-byte big-endian message boundary is validated, actual bytes are bounded,
   SHA-256 is computed, and a random storage path is assigned. This step does not
   claim semantic order-book validity.
3. A replay job records an immutable risk-parameter snapshot and durable queued
   intent. API callers can use `Idempotency-Key` (8–128 ASCII alphanumerics/_.:-)
   to safely repeat a submission after losing an HTTP response.
4. Celery delivers the job. A transactional state claim prevents duplicate
   execution; owner, dataset and platform-state locks use a consistent order.
5. The worker rechecks file integrity, then invokes the real C++ executable using
   argument arrays, no shell, and a minimal environment without service secrets.
   A fresh Python resource wrapper imposes Linux address-space, CPU, file-size
   and core-dump limits before exec. Linux `PR_SET_PDEATHSIG=SIGKILL`, with an
   expected-parent PID recheck, stops the parser when its Celery worker dies.
   Failure to install this protection aborts execution. This is not a complete
   sandbox: container isolation and kernel/filesystem hardening are still required.
6. JSON schema essentials are validated and results are committed with audit
   events. Errors, cancellation, timeout and lost workers remain explicit states.

Public ITCH trade events are not account fills. Default `mode=replay` contains no
account PnL or fills. `mode=simulation` invokes the engine's explicit automatic
market-making simulator (`--simulate`), and records its simulated orders, fills,
inventory, fees, rebates and net PnL. These are model outputs, never broker events
or promised profit. Simulation is single-symbol only; a second stock locate is
rejected. `elapsed_ns` is batch runtime, not a live tick-to-send benchmark.

The immutable `simulation_parameters` accepts the following bounded integers:
`latency_ns`, `cancel_latency_ns` (1,000–10,000,000,000 ns),
`fee_per_share_units`, `min_fee_per_order_units`, `slippage_buffer_units`,
`min_net_edge_units` (0–1,000,000,000 units of $0.0001),
`rebate_per_share_units` (0–1,000,000 units of $0.0001), and
`simulation_window_ns` (1,000,000–60,000,000,000 ns; quote maximum lifetime, NOT
the input-file duration). Defaults are 1 ms / 1 ms / $0.003 per share / zero rebate
/ one second quote lifetime. Fee and rebate assumptions are explicit user inputs,
not claims about an actual broker schedule. The UI converts ledger units into USD.

`RiskProfile.tick_size` is snapshotted for both replay and simulation and passed
as `--tick-size`. Its unit is 1/10000 USD, with an accepted range of 1–100000.
The initial value 100 ($0.01) is explicitly a research assumption, not a claim
that all US instruments or price bands use a one-cent tick. Configure it for the
instrument and venue in the input dataset. Migration preserves historical jobs'
actual old default of 100; it does not reintroduce cleared data after deletion.

The ledger retains unclosed inventory and end-of-file open orders; it does not
invent a final liquidation. When `pnl_mark_valid=false`, the interface withholds
net/unrealized PnL and drawdown as invalid estimates. Inventory and loss controls
apply to simulated executions; real-account protection still requires separate
broker integration and acceptance.

## Routes

Web: `/`, `/datasets/`, `/datasets/<uuid>/`, `/jobs/`, `/jobs/<uuid>/`,
`/risk/`, `/feedback/`, `/operations/`.

Session-authenticated API (CSRF required for mutation):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/session/` | Identity/capability state and masked CSRF token |
| GET/POST | `/api/v1/datasets/` | Paginated list / multipart upload |
| GET/DELETE | `/api/v1/datasets/<uuid>/` | Private metadata / deletion intent |
| GET/POST | `/api/v1/jobs/` | Paginated list / JSON `{dataset_id, mode?, simulation_parameters?}` submission |
| GET | `/api/v1/jobs/<uuid>/` | Status, parameter snapshot and terminal result |
| POST | `/api/v1/jobs/<uuid>/cancel/` | Cancel queued or request running cancellation |
| POST | `/api/v1/jobs/<uuid>/retry/` | New job for a failed/canceled parent |

All API responses use `{success, code, message, data}` with corresponding HTTP
status. Queue delivery failure returns 503 **with the persisted job identity**;
do not blindly create another job. The periodic dispatcher can deliver it later.
Repeat the request with the same idempotency key, or poll the existing ID.

Dataset raw files are served only by an ownership-checked download view. Never
map MEDIA_ROOT to a public static URL or add an Nginx alias for private media.

## Permissions

Ordinary/member/premium users can access only their own records. Verification is
required to upload and run. Session revocation is provided by accounts middleware.
Operations requires verified active staff plus the explicit Django permission
`trading.change_platformsafetystate` and password reauthentication. Membership
or an admin role alone does not grant this operational permission. State mutation
through Django's generic admin is disabled. Feature-flag editing and feedback
responses use separate Django model permissions and produce audit records.

`replay` controls research submissions; `simulation` additionally gates simulation
mode. Absent means the baseline feature is available. Creating a flag with
`enabled=false` closes relevant submissions; rollout uses a stable per-user SHA-256
bucket. Flags cannot be deleted in admin because deletion restores the baseline.

Operations password checks have separate user/IP rate limits and redact the
password in Django exception reports. Account deletion clears the risk profile,
job results/parameter snapshots, and feedback content; file purge is asynchronous.
Owner mutations lock and recheck the user to prevent racing account deletion.

## Worker and settings

- `ENGINE_BINARY`: trusted absolute path to built trading-replay.
- `REPLAY_TIMEOUT_SECONDS`: default 120.
- `REPLAY_MEMORY_BYTES`: default 1 GiB, per engine process address-space ceiling.
- `MAX_DATASET_BYTES`: default 256 MiB, actual streamed-byte cap.
- `MAX_USER_DATASET_BYTES`: default 1 GiB (including pending deletion).
- `MAX_USER_DATASETS`: default 100.
- `MAX_ACTIVE_REPLAYS_PER_USER`: default 2.
- `MAX_REPLAY_ATTEMPTS`: default 3.

Required periodic tasks:

- `trading.tasks.reconcile_replays`, every 60 seconds: retry durable queued intents
  and fail jobs whose worker heartbeat is stale.
- `trading.tasks.purge_deleted_datasets`, every 300 seconds: remove marked private
  files and record the purge timestamp; filesystem failures are retried later.

The worker updates status in PostgreSQL; Redis is not the source of truth for
job state. Do not run multiple workers without tested database concurrency and
container memory limits. Linux is required for resource-bound engine execution.
Task deliveries carry explicit `contract_version=4`; older workers reject the
new contract instead of ignoring the changed pricing economics. Old queued
simulation snapshots lacking the three new cost fields fail explicitly with
`SIMULATION_CONTRACT_CHANGED`; historical completed results are not rewritten.
Old automatic configurations cannot restart under different economics: create
a new configuration after reviewing its costs. The scheduler faults/latches old
running configurations before creating another job.
Deploy worker support before exposing new features and retain outbox reconciliation
during rolling upgrades. Drain old workers before rollback.

## Authorized automatic runs

`/automations/` provides persistent configurations for **periodic historical
simulation**. Creation is always STOPPED. A verified active owner must explicitly
start it with password reauthentication. Each run uses the configured private
dataset, immutable risk snapshot and simulation assumptions. Every run starts an
independent simulation account; repeated historical results are never compounded
or represented as new real profits, live market input or actual broker fills.

The scheduler task is `trading.tasks_automations.tick_automations`, every 15 seconds.
Register the module using `CELERY_IMPORTS=("trading.tasks_automations",)`.
It scans a bounded, fair batch (default 100), obtains owner/dataset/platform/
automation/job locks in that order, and creates at most one pending run per
configuration. A partial database unique constraint independently prevents two
QUEUED/RUNNING jobs from belonging to the same automation.

Configuration limits are interval 60–86400 seconds and max_runs 1–1000, default
300 seconds and ten runs. Quotas default to 20 stored configurations and three
RUNNING configurations per user. Job concurrency and storage quotas still apply.
Missed intervals are not replayed; the next planned time is based on actual
scheduler time, and the run budget counts created attempts, including failures.
Existing active jobs prevent overlap. Queue delivery failure retains the same
durable job rather than consuming additional runs on every scheduler tick.

State behavior:

- STOPPED: created or explicitly stopped; no automatic startup.
- RUNNING: authorized scheduled simulations, until stopped or a gate is reached.
- PAUSED: account/feature/platform gate, invalid valuation, or simulated risk kill.
- FAULTED: three consecutive failed/canceled runs or invalid persistent config.

Simulated risk kills, invalid marks and consecutive-failure faults latch. Neither
market recovery nor restarting the scheduler clears them. The owner must inspect
the cause, reauthenticate and explicitly acknowledge the latch before resuming;
the remaining run budget is not reset. Reaching max_runs stops further runs.
Stopping cancels queued jobs and requests cancellation of running subprocesses;
terminal job status is the evidence of completion. The worker rechecks automation
state, generation and feature gates before invoking the engine.

The `automations` feature flag additionally gates this workflow, alongside
`replay` and `simulation`. Disabling it pauses running configurations at scheduler
inspection and prevents queued work from starting; enabling it does not restart
them automatically. Account deletion stops/redacts configurations and respects
running-job cancellation; dataset deletion makes dependent configurations unusable.

API endpoints (session authentication and CSRF apply):

| Method | Path | Contract |
|---|---|---|
| GET/POST | `/api/v1/automations/` | Own list / create STOPPED configuration |
| GET | `/api/v1/automations/<uuid>/` | Own state, parameters, schedule, latest job |
| POST | `/api/v1/automations/<uuid>/start/` | Password + optional explicit latch acknowledgment |
| POST | `/api/v1/automations/<uuid>/stop/` | Password; stops future work and requests cancellation |

Creation JSON: `{name, dataset_id, interval_seconds?, max_runs?,
simulation_parameters?}`. `mode` can only be `simulation` if supplied. Start/stop
JSON: `{password, acknowledge_latch?: boolean}`. All POSTs require an
`Idempotency-Key`. Controls have user/IP rate limits, and passwords are neither
stored in receipts nor audit data. Durable command receipts ensure retrying an
old start request **after a stop** does not restart the automation. The response
contains the current configuration plus the command's original outcome; they
can legitimately differ when an old command is replayed.

The dashboard and operations page show persisted configuration counts and actual
job states; there is no fabricated cumulative profit metric. Database or broker
outages do not manufacture successful runs. Scheduler exceptions are logged;
external health monitoring remains required for a stopped worker or scheduler.

## Verification

From backend: `../.venv/bin/python manage.py test trading --settings=config.testing`.
Build the real C++ executable before testing. The suite includes genuine engine
subprocess roundtrips/checksum reproducibility, explicit test-only slow executables
for cancellation/timeout, private data isolation, CSRF, durable dispatch failure,
quota checks, parameter snapshots, idempotency, deletion and feature gates.
Real-engine tests are visibly skipped if the executable is missing; a run with
skips is not evidence of end-to-end engine integration.

Production PostgreSQL locking, real Celery/Redis delivery, licensed market data,
deployment recovery and live broker connectivity need separate environment-level
acceptance. Passing these local tests is not a production release declaration.


## Transaction-cost-aware research and assessments

New simulation runs use `displayed_fifo_conservative_v2_cost_aware` with cost
policy `conservative_one_share_v2`. The minimum quote half-width includes per-share
fees, the entire minimum order commission conservatively allocated to one filled
share, the user-configured slippage buffer and minimum net edge. Rebates never
reduce this floor. The minimum commission is charged once per order as
`max(minimum, cumulative_filled_shares * fee_per_share)` using incremental ledger
charges on partial fills. Defaults for the three new fields are zero; those
defaults do not imply that broker trading has zero costs. Slippage buffer and
minimum edge are quote constraints, not extra realized expenses.

Web: `/assessments/` (own history and creation), `/assessments/<uuid>/` (immutable
evidence). API: GET/POST `/api/v1/assessments/`, GET `/api/v1/assessments/<uuid>/`.
POST JSON is `{job_ids: [1..50 UUID strings], extra_cost_per_share_units?: 10}`;
optional `Idempotency-Key` follows the replay format. Responses use the standard
envelope; creation returns 201, an identical idempotent retry 200. `data` includes
`id`, `result`, `snapshot`, `snapshot_sha256`, policy version and creation time.
`execution_authorized` is always false, including every assessment classification.
No endpoint edits an assessment or grants execution rights.

The verified active owner is rechecked under the same User row lock used for
account deletion. CSRF, shared-cache user/IP rate limits, a stored-assessment
quota, owner filtering and audit events apply. Admin is view-only; even model
`save()` rejects updates. Privacy erasure explicitly redacts snapshots, results,
hashes and idempotency keys and makes the record inaccessible. User export
includes only that owner's non-redacted assessments.

A selected job must be a succeeded simulation with completed timestamps, valid
result structure, server-owned SHA256 input identity, and consistent immutable
risk and simulation snapshots. Arithmetic, currency/units, quantity/inventory
parity, possible order counts, pricing floor, executed configuration and fees
are cross-checked. Per-order minimum commission is checked using the reported
charged-order count, conservative fee bounds and the required minimum number
of buy/sell orders. These are necessary aggregate ledger checks; they do not
reconstruct a complete per-order execution ledger. Missing fields are rejected,
never defaulted to zero. The engine result's canonical SHA256 and execution
parameters are retained in the immutable evidence snapshot.

Identical dataset SHA256 hashes are rejected, including repeated automatic runs;
no highest-profit representative is chosen. Different SHA256 files can overlap
in time. Every run resets its simulation account, so sums are sums of separate
scenarios, not portfolio or continuous account performance. Model versions,
schema versions, full risk snapshots and normalized execution assumptions must
match. Legacy v1 completed results can be assessed separately and are visibly
labeled legacy; they can never be combined with v2 cost-aware results.

Net PnL is independently checked as realized + unrealized - fees + rebates. The
fixed-fill stress scenario subtracts **all assumed rebates** and an additional
0..10000 units per actually filled share (both buy and sell shares counted),
default10 ($0.001/share). Baseline fees and minimum commissions are not deducted
again. This scenario does not rerun fills under changed latency or order flow.

Invalid valuation makes aggregate net/unrealized/stressed results unavailable;
risk kill, no fills and unclosed inventory/orders prevent a positive sample
classification. Negative, flat, cost-sensitive, insufficient and positive
research samples have separate states. Default research thresholds are five
different captures and 100 filled shares, configurable with
`ASSESSMENT_MIN_CAPTURES` and `ASSESSMENT_MIN_FILLED_QUANTITY`;
`MAX_ASSESSMENTS_PER_USER` defaults1000. Thresholds are snapshotted and are not
statistical proof, independent-day evidence, out-of-sample testing, or permission
to trade. Positive samples only support further paper research.
