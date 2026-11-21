#pragma once
// Remaining core data structures from the design doc §6 (compiled + size-checked).
#include "risk.hpp"
#include <concepts>
#include <sys/mman.h>

namespace hft {

// ───────────────────────── Market snapshot (per symbol, owned by shard) ─────────────────────────
inline constexpr int kDepth = 5;
struct alignas(kCacheLine) MarketSnapshot {
  // line 0: top of book + status — touched by every decision
  Price    bid_px0, ask_px0;     // best bid / ask (0 = empty side)
  Qty      bid_qty0, ask_qty0;
  uint64_t last_exch_ts;         // ITCH ns since midnight of last applied event
  uint64_t last_seq;             // MoldUDP64 seq of last applied event
  uint32_t bid_cnt0, ask_cnt0;   // order count at best (queue length proxy)
  uint8_t  trading_state;        // 'T','H','P','Q'
  uint8_t  luld_flags;
  uint8_t  ssr;                  // Reg SHO Rule 201 in effect
  uint8_t  book_valid;
  uint32_t pad_;
  // lines 1..3: depth (SoA inside the struct: the imbalance loop reads qty arrays only)
  alignas(kCacheLine) Price bid_px[kDepth];
  Price    ask_px[kDepth];
  Qty      bid_qty[kDepth];
  Qty      ask_qty[kDepth];
  Price    last_trade_px;
  Qty      last_trade_qty;
};
static_assert(sizeof(MarketSnapshot) == 4 * kCacheLine);
static_assert(offsetof(MarketSnapshot, bid_px) == kCacheLine);

// ───────────────────────── Quotes & OMS orders ─────────────────────────
enum class QuoteSlot : uint8_t { Bid = 0, Ask = 1 };
struct Quote {                   // desired quote for one side, written by strategy
  Price px;
  Qty   qty;                     // 0 => side off
  uint64_t decided_tsc;
  uint64_t md_seq;               // market event seq the decision was based on
};

enum class OrdState : uint8_t {
  Free, PendingNew, Live, PendingReplace, PendingCancel, Filled, Canceled, Rejected, Unknown
};
struct alignas(kCacheLine) Order {   // OMS order, one cache line
  uint64_t cl_ord_id;            // [gen:12 | pool idx:20 | ...] encodes pool index -> O(1) lookup
  uint64_t exch_order_id;        // venue / broker id (OUCH order ref, IBKR permId)
  uint64_t corr_id;              // correlation id of triggering market event
  Price    px;                   // live price
  Price    pending_px;           // in-flight replace target
  Qty      qty;                  // original qty
  Qty      leaves;               // live leaves
  Qty      pending_qty;          // in-flight replace qty
  // 64 bytes so far ... next line
  uint64_t sent_tsc;             // for ack-timeout + latency stats
  Qty      cum_qty;
  SymbolId sym;
  Side     side;
  OrdState state;
  QuoteSlot slot;
  uint8_t  gw;                   // gateway / session id
  uint8_t  replace_count;
  uint8_t  pad_;
};
static_assert(sizeof(Order) == 128);  // two lines: hot (price/qty) + warm (timing)

// ───────────────────────── Execution report (normalized across gateways) ─────────────────────────
enum class ExecType : uint8_t {
  Ack, Reject, PartialFill, Fill, Canceled, CancelReject, Replaced, ReplaceReject,
  Expired, Broken, Restated, StatusUnknown
};
enum class Liquidity : uint8_t { Unknown, Added, Removed, Routed, Auction };
struct alignas(kCacheLine) ExecutionReport {
  uint64_t cl_ord_id;
  uint64_t exch_order_id;
  uint64_t exec_id;              // OUCH match number / hash64(IBKR execId) for dedup
  uint64_t recv_tsc;
  Price    last_px;
  uint32_t last_qty;             // shares fit in 32 bits; keeps the report in one cache line
  uint32_t leaves;
  int32_t  reason;               // normalized reject/cancel reason
  SymbolId sym;
  Side     side;
  ExecType type;
  Liquidity liq;
  uint8_t  gw;
  uint8_t  pad_[6];
};
static_assert(sizeof(ExecutionReport) == 64);

// ───────────────────────── Position / inventory / PnL ─────────────────────────
struct Position {
  Qty      qty = 0;              // signed
  Price    avg_px = 0;           // 1e-4 USD, average cost of open position
  Qty      bought = 0, sold = 0; // day totals (shares)
  int64_t  realized = 0;         // 1e-4 USD
};

enum class InvZone : uint8_t { Normal, Warning, Critical, Breach };
struct InventoryState {
  Qty      q = 0;                // == Position::qty
  Qty      soft, hard, limit;    // |q| thresholds (shares), soft < hard < limit
  Qty      hysteresis;           // shares below a threshold before de-escalating
  InvZone  zone = InvZone::Normal;
  uint64_t zone_since_tsc = 0;
  uint64_t inventory_age_tsc = 0;// time since |q| was last ~0
  double   skew = 0.0;           // q / limit in [-1,1], cached for pricing
};

struct alignas(kCacheLine) PnLState {
  int64_t realized = 0;          // all 1e-4 USD
  int64_t unrealized = 0;        // marked conservatively (long@bid, short@ask)
  int64_t fees = 0;              // commissions + exchange take fees + SEC/TAF + clearing (estimated)
  int64_t rebates = 0;           // maker rebates (estimated, reconciled later)
  int64_t net = 0;               // realized + unrealized - fees + rebates
  int64_t peak_net = 0;          // intraday high-water mark (for drawdown)
  int64_t drawdown = 0;          // peak_net - net
  uint64_t updated_tsc = 0;
};

// ───────────────────────── Strategy state (per symbol) ─────────────────────────
enum class QuoteMode : uint8_t { Disabled, Pulled, OneSidedBid, OneSidedAsk, TwoSided };
struct alignas(kCacheLine) StrategyState {
  Quote    desired[2];           // [Bid, Ask]
  uint32_t live_order[2];        // OMS pool index, kNil if none
  uint64_t last_change_tsc[2];   // for min quote life / hysteresis
  uint64_t cooldown_until_tsc;   // after pull / reject storm
  QuoteMode mode;
  uint8_t  dirty;                // TOB / inventory changed since last reconcile
  uint8_t  reject_streak;
  uint8_t  pad_[5];
};

// ───────────────────────── Execution gateway abstraction ─────────────────────────
enum class SendResult : uint8_t { Ok, Throttled, Disconnected, Invalid };

struct ExecSinkProbe { void on_exec_report(const ExecutionReport&) noexcept {} };

// Static interface (hot path): no virtual dispatch, fully inlinable.
template <class G>
concept ExecutionGateway = requires(G g, const OrderRequest& r, ExecSinkProbe& sink) {
  { g.send_new(r) } noexcept -> std::same_as<SendResult>;
  { g.send_replace(r) } noexcept -> std::same_as<SendResult>;
  { g.send_cancel(r) } noexcept -> std::same_as<SendResult>;
  { g.cancel_all() } noexcept -> std::same_as<SendResult>;
  { g.poll(sink) } noexcept -> std::same_as<int>;    // drains exec reports into sink
  { g.healthy() } noexcept -> std::same_as<bool>;
};

// Dynamic interface (tools, Architecture A, tests): one virtual call is irrelevant
// next to an IBKR round trip of milliseconds.
class IExecutionGateway {
 public:
  virtual ~IExecutionGateway() = default;
  virtual SendResult send_new(const OrderRequest&) noexcept = 0;
  virtual SendResult send_replace(const OrderRequest&) noexcept = 0;
  virtual SendResult send_cancel(const OrderRequest&) noexcept = 0;
  virtual SendResult cancel_all() noexcept = 0;
  virtual int poll_into(void (*cb)(void*, const ExecutionReport&), void* ctx) noexcept = 0;
  virtual bool healthy() const noexcept = 0;
};

// Example: simulated gateway satisfying the concept (used by tests/backtest).
class SimGateway {
 public:
  SendResult send_new(const OrderRequest&) noexcept { ++sent_; return SendResult::Ok; }
  SendResult send_replace(const OrderRequest&) noexcept { ++sent_; return SendResult::Ok; }
  SendResult send_cancel(const OrderRequest&) noexcept { ++sent_; return SendResult::Ok; }
  SendResult cancel_all() noexcept { return SendResult::Ok; }
  template <class Sink> int poll(Sink&) noexcept { return 0; }
  bool healthy() noexcept { return true; }
  uint64_t sent_ = 0;
};
static_assert(ExecutionGateway<SimGateway>);

// ───────────────────────── Memory: hugepage arena (startup only) ─────────────────────────
class HugeArena {
 public:
  explicit HugeArena(std::size_t bytes) noexcept : size_(bytes) {
    base_ = static_cast<std::byte*>(::mmap(nullptr, bytes, PROT_READ | PROT_WRITE,
                                           MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB, -1, 0));
    if (base_ == MAP_FAILED) {     // fall back to normal pages (dev boxes); production: fail startup
      base_ = static_cast<std::byte*>(::mmap(nullptr, bytes, PROT_READ | PROT_WRITE,
                                             MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
      huge_ = false;
    }
    if (base_ != MAP_FAILED) {
      std::memset(base_, 0, bytes);   // prefault every page now, not on the hot path
      ::mlock(base_, bytes);          // + mlockall(MCL_CURRENT|MCL_FUTURE) at process start
    }
  }
  ~HugeArena() { if (base_ && base_ != MAP_FAILED) ::munmap(base_, size_); }
  HugeArena(const HugeArena&) = delete;
  HugeArena& operator=(const HugeArena&) = delete;
  // Monotonic bump allocation; never freed individually; reset only at end of day.
  void* allocate(std::size_t n, std::size_t align = kCacheLine) noexcept {
    std::size_t p = (used_ + align - 1) & ~(align - 1);
    if (p + n > size_) return nullptr;
    used_ = p + n;
    return base_ + p;
  }
  template <class T, class... A> T* make(A&&... a) noexcept {
    void* p = allocate(sizeof(T), alignof(T) > kCacheLine ? alignof(T) : kCacheLine);
    return p ? ::new (p) T(static_cast<A&&>(a)...) : nullptr;
  }
  bool huge() const noexcept { return huge_; }
  bool ok() const noexcept { return base_ != MAP_FAILED && base_ != nullptr; }
 private:
  std::byte* base_ = nullptr;
  std::size_t size_, used_ = 0;
  bool huge_ = true;
};

}  // namespace hft
