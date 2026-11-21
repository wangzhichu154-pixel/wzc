#pragma once
#include "ref.hpp"
#include <cmath>

namespace hft {

// ───────────────────────── Orders / risk state ─────────────────────────
enum class OrderAction : uint8_t { New, Replace, Cancel };
enum class TimeInForce : uint8_t { Day, Ioc };
enum class Intent : uint8_t { Quote, ReduceInventory, Liquidate };

struct OrderRequest {
  uint64_t cl_ord_id;     // monotonic, unique per session
  uint64_t corr_id;       // correlation id of the triggering market event
  uint64_t orig_cl_ord_id;// Replace/Cancel target
  Price    px;            // 1e-4 USD
  Qty      qty;           // shares (> 0)
  Qty      orig_leaves;   // Replace: leaves of the order being replaced
  SymbolId sym;
  Side     side;
  OrderAction action;
  TimeInForce tif;
  Intent   intent;
  bool     post_only;
};

enum class RiskVerdict : uint8_t {
  Ok = 0, KillSwitch, StrategyDisabled, SymbolHalted, BookInvalid, StaleMarketData,
  GatewayDown, BadQty, BadPrice, MaxOrderSize, PriceCollar, WouldCross,
  MaxPosition, InventoryZone, ShortSaleRestricted, MaxOpenOrders, GrossExposure,
  NetExposure, DailyLoss, OrderRate, CancelRate, ReplaceRate, MsgRate, Duplicate
};

// Global flags: written only by Risk/Supervisor, read by every trading thread.
enum : uint64_t {
  kBlkKill        = 1ull << 0,  // kill switch tripped (latched)
  kBlkStrategyOff = 1ull << 1,
  kBlkGatewayDown = 1ull << 2,
  kBlkMdGap       = 1ull << 3,  // feed-level gap / recovery in progress
  kBlkClockBad    = 1ull << 4,
  kBlkLatency     = 1ull << 5,  // latency spike breaker
  kBlkRejectRate  = 1ull << 6,
  kBlkLogBackpressure = 1ull << 7,  // audit log cannot keep up -> fail safe
  kBlkReduceOnly  = 1ull << 8,  // not a hard block: only risk-reducing orders allowed
};
enum : uint32_t {
  kSymHalted = 1u << 0, kSymBookInvalid = 1u << 1, kSymSsr = 1u << 2,
  kSymNoShort = 1u << 3, kSymBuyBlocked = 1u << 4, kSymSellBlocked = 1u << 5,
};

struct alignas(kCacheLine) SymbolRisk {   // exactly one cache line, owned by one thread
  Qty      position;        // signed filled position
  Qty      open_buy;        // sum of leaves of live/pending buy orders
  Qty      open_sell;
  Qty      max_position;    // |worst-case position| limit (shares)
  Price    ref_px;          // collar reference (mid / last trade), 1e-4 USD
  Price    best_bid, best_ask;
  uint32_t last_md_ts32;    // low 32 bits of last MD ns (wraps every ~4.29s, ok for staleness <1s)
  uint16_t open_orders;
  uint16_t flags;           // kSym*
};
static_assert(sizeof(SymbolRisk) == 64);

struct RiskLimits {
  Qty      max_order_qty;
  uint16_t max_open_orders_per_sym;
  int64_t  collar_bps;          // max deviation from ref, basis points
  Price    collar_min_abs;      // floor for collar on low-priced names
  int64_t  max_gross_notional;  // 1e-4 USD units
  int64_t  max_net_notional;
  int64_t  daily_loss_limit;    // positive number, 1e-4 USD
  uint32_t md_stale_ns;
};

struct alignas(kCacheLine) RiskState {
  const std::atomic<uint64_t>* global_block;  // shared, read-mostly line
  SymbolRisk* sym;                            // indexed by book slot
  int64_t gross_worst_notional;               // Σ |worst-case position| * px  (shard budget)
  int64_t net_notional;                       // Σ position * px
  int64_t net_pnl;                            // realized + unrealized - fees + rebates
  RiskLimits lim;
  Gcra order_rate{20, 10}, cancel_rate{40, 20}, replace_rate{40, 20};
  Gcra msg_rate{45, 5};                       // broker-wide message budget
  uint64_t last_cl_ord_id;
};

// ───────────────────────── pre-trade check ─────────────────────────
// Ordering principle: (1) one load that covers every global "stop" condition,
// (2) per-symbol flags, (3) cheap field validation, (4) arithmetic limits,
// (5) rate limiting last (peek only, commit after the order is accepted).
// Cancels bypass position/PnL limits: they can only reduce risk.
[[nodiscard]] inline RiskVerdict
pre_trade_check(const OrderRequest& r, const RiskState& rs, uint64_t now_ns) noexcept {
  const uint64_t blk = rs.global_block->load(std::memory_order_acquire);
  const SymbolRisk& s = rs.sym[r.sym];

  if (r.action == OrderAction::Cancel) [[unlikely]] {
    // never block a cancel for risk reasons; only the broker-wide budget applies
    return rs.msg_rate.can_acquire(now_ns) ? RiskVerdict::Ok : RiskVerdict::MsgRate;
  }

  // (1) global stop conditions — single predictable branch in the normal case
  constexpr uint64_t kHard = kBlkKill | kBlkStrategyOff | kBlkGatewayDown | kBlkMdGap |
                             kBlkClockBad | kBlkLatency | kBlkRejectRate | kBlkLogBackpressure;
  if (blk & kHard) [[unlikely]] {
    if (blk & kBlkKill) return RiskVerdict::KillSwitch;
    if (blk & kBlkGatewayDown) return RiskVerdict::GatewayDown;
    if (blk & kBlkMdGap) return RiskVerdict::BookInvalid;
    return RiskVerdict::StrategyDisabled;
  }
  // (2) per-symbol trading state + market data freshness
  if (s.flags & (kSymHalted | kSymBookInvalid)) [[unlikely]]
    return (s.flags & kSymHalted) ? RiskVerdict::SymbolHalted : RiskVerdict::BookInvalid;
  if (uint32_t(now_ns) - s.last_md_ts32 > rs.lim.md_stale_ns) [[unlikely]]
    return RiskVerdict::StaleMarketData;

  // (3) field sanity / fat finger
  if (r.qty <= 0 || r.qty > rs.lim.max_order_qty) [[unlikely]]
    return r.qty <= 0 ? RiskVerdict::BadQty : RiskVerdict::MaxOrderSize;
  if (r.px <= 0) [[unlikely]] return RiskVerdict::BadPrice;
  if (r.cl_ord_id <= rs.last_cl_ord_id) [[unlikely]] return RiskVerdict::Duplicate;
  {
    const Price dev = r.px > s.ref_px ? r.px - s.ref_px : s.ref_px - r.px;
    const Price collar = std::max<Price>(s.ref_px * rs.lim.collar_bps / 10'000, rs.lim.collar_min_abs);
    if (dev > collar) [[unlikely]] return RiskVerdict::PriceCollar;
  }
  const bool buy = r.side == Side::Buy;
  if (r.post_only) {
    if (buy ? (s.best_ask > 0 && r.px >= s.best_ask) : (s.best_bid > 0 && r.px <= s.best_bid)) [[unlikely]]
      return RiskVerdict::WouldCross;
  }

  // (4) worst-case position / inventory / exposure (assume every open order fills)
  const Qty add = r.qty - (r.action == OrderAction::Replace ? r.orig_leaves : 0);
  const Qty worst = buy ? s.position + s.open_buy + add : s.position - s.open_sell - add;
  const bool increases_risk = buy ? worst > 0 : worst < 0;
  if ((worst > s.max_position) | (worst < -s.max_position)) [[unlikely]] return RiskVerdict::MaxPosition;
  if (increases_risk && ((blk & kBlkReduceOnly) || (s.flags & (buy ? kSymBuyBlocked : kSymSellBlocked)))) [[unlikely]]
    return RiskVerdict::InventoryZone;
  if (!buy && worst < 0 && (s.flags & (kSymNoShort | kSymSsr))) [[unlikely]] {
    if (s.flags & kSymNoShort) return RiskVerdict::ShortSaleRestricted;
    if (r.px <= s.best_bid) return RiskVerdict::ShortSaleRestricted;  // Rule 201: must be above NBB (use NBB, not venue bid, in prod)
  }
  if (r.action == OrderAction::New && s.open_orders >= rs.lim.max_open_orders_per_sym) [[unlikely]]
    return RiskVerdict::MaxOpenOrders;
  if (increases_risk) {
    const int64_t notional = add * r.px;  // fits: 1e6 sh * 1e8 (=$10k) = 1e14
    if (rs.gross_worst_notional + notional > rs.lim.max_gross_notional) [[unlikely]] return RiskVerdict::GrossExposure;
    const int64_t net = rs.net_notional + (buy ? notional : -notional);
    if (net > rs.lim.max_net_notional || net < -rs.lim.max_net_notional) [[unlikely]] return RiskVerdict::NetExposure;
    if (rs.net_pnl <= -rs.lim.daily_loss_limit) [[unlikely]] return RiskVerdict::DailyLoss;
  }

  // (5) rate limits (peek; commit_after_send() spends the token)
  const Gcra& lim = r.action == OrderAction::New ? rs.order_rate : rs.replace_rate;
  if (!lim.can_acquire(now_ns)) [[unlikely]]
    return r.action == OrderAction::New ? RiskVerdict::OrderRate : RiskVerdict::ReplaceRate;
  // keep >= 2 broker messages in reserve for cancels
  if (rs.msg_rate.available(now_ns) <= 2) [[unlikely]] return RiskVerdict::MsgRate;
  return RiskVerdict::Ok;
}

inline bool pre_trade_check_bool(const OrderRequest& r, const RiskState& rs, uint64_t now) noexcept {
  return pre_trade_check(r, rs, now) == RiskVerdict::Ok;
}

// ───────────────────────── Daily loss kill switch ─────────────────────────
enum class KillState : uint8_t { Armed, Warning, ReduceOnly, Tripped, Locked };

struct KillConfig {
  int64_t daily_loss_limit;    // > 0
  int64_t warn_pct = 60;       // % of limit -> Warning
  int64_t reduce_pct = 85;     // % of limit -> ReduceOnly
};

class KillSwitch {
 public:
  KillSwitch(KillConfig c, std::atomic<uint64_t>* global_block) noexcept : c_(c), blk_(global_block) {}
  // Called synchronously on every PnL change (fill, mark). Returns true on the transition into Tripped.
  bool on_pnl(int64_t net_pnl) noexcept {
    if (state_ >= KillState::Tripped) return false;  // latched: only Supervisor can leave
    const int64_t loss = -net_pnl;
    if (loss >= c_.daily_loss_limit) {
      state_ = KillState::Tripped;
      blk_->fetch_or(kBlkKill, std::memory_order_acq_rel);  // (1) new orders rejected from now on
      return true;                                           // caller runs cancel-all etc.
    }
    const KillState next = loss * 100 >= c_.daily_loss_limit * c_.reduce_pct ? KillState::ReduceOnly
                         : loss * 100 >= c_.daily_loss_limit * c_.warn_pct   ? KillState::Warning
                                                                            : KillState::Armed;
    // no automatic de-escalation within the day (avoid flapping)
    if (next > state_) {
      state_ = next;
      if (state_ == KillState::ReduceOnly) blk_->fetch_or(kBlkReduceOnly, std::memory_order_acq_rel);
    }
    return false;
  }
  // Trip for a non-PnL reason (drawdown breach, operator, watchdog). Returns true on transition.
  bool force_trip() noexcept {
    if (state_ >= KillState::Tripped) return false;
    state_ = KillState::Tripped;
    blk_->fetch_or(kBlkKill, std::memory_order_acq_rel);
    return true;
  }
  void on_actions_complete() noexcept { if (state_ == KillState::Tripped) state_ = KillState::Locked; }
  KillState state() const noexcept { return state_; }

  // Only reachable from the Supervisor control channel (authenticated, audited).
  // Token is minted by the supervisor process; strategy code never holds one.
  struct SupervisorToken { uint64_t operator_id; uint64_t nonce; };
  bool supervisor_reset(const SupervisorToken& t, bool positions_reconciled, bool md_healthy) noexcept {
    if (state_ != KillState::Locked || !positions_reconciled || !md_healthy || t.operator_id == 0) return false;
    state_ = KillState::Armed;
    blk_->fetch_and(~(kBlkKill | kBlkReduceOnly), std::memory_order_acq_rel);
    return true;
  }
 private:
  KillConfig c_;
  std::atomic<uint64_t>* blk_;
  KillState state_ = KillState::Armed;
};

// ───────────────────────── Quoting (pricing core) ─────────────────────────
struct Features {          // computed incrementally per symbol
  double mid, microprice, imbalance /*[-1,1]*/, trade_imbalance /*[-1,1]*/;
  double momentum /*$ per s EWMA*/, sigma /*$ per sqrt(s)*/, depth_shares;
};
struct QuoteParams {
  double tick, half_spread_min_ticks, k_vol, k_inv_spread, k_liq, k_flow;
  double w_micro, w_imb, w_flow, w_mom, horizon_s;
  double skew_ticks_at_limit;       // reservation shift (ticks) at |q| == limit
  double fee_edge;                  // $ per share needed to break even (fees - rebates + min edge)
  int64_t base_size, lot, inv_limit;
};
struct QuotePair { Price bid_px, ask_px; Qty bid_qty, ask_qty; bool bid_on, ask_on; };

inline Price to_fixed(double usd) noexcept { return Price(std::llround(usd * 10'000.0)); }

inline QuotePair compute_quotes(const Features& f, double best_bid, double best_ask,
                                int64_t inv, const QuoteParams& p) noexcept {
  const double half_book = 0.5 * (best_ask - best_bid);
  // 1) fair value: mid + bounded short-horizon adjustments
  double fair = f.mid + p.w_micro * (f.microprice - f.mid) + p.w_imb * f.imbalance * half_book +
                p.w_flow * f.trade_imbalance * p.tick + p.w_mom * f.momentum * p.horizon_s;
  fair = std::clamp(fair, best_bid, best_ask);  // never let a signal push fair outside the touch
  // 2) inventory skew -> reservation price
  const double qn = std::clamp(double(inv) / double(p.inv_limit), -1.0, 1.0);
  const double reservation = fair - qn * p.skew_ticks_at_limit * p.tick;
  // 3) half spread from volatility, inventory, liquidity, flow toxicity
  double hs = p.k_vol * f.sigma * std::sqrt(p.horizon_s) + p.k_inv_spread * std::fabs(qn) * p.tick +
              p.k_liq * p.tick / std::max(1.0, f.depth_shares / double(p.base_size)) +
              p.k_flow * std::fabs(f.trade_imbalance) * p.tick;
  hs = std::max({hs, p.half_spread_min_ticks * p.tick, p.fee_edge});
  // asymmetric widening on the risk-increasing side
  const double hs_bid = hs * (1.0 + std::max(qn, 0.0));
  const double hs_ask = hs * (1.0 + std::max(-qn, 0.0));
  // 4) snap to tick grid away from the reservation price, never cross the touch (post-only)
  double bid = std::floor((reservation - hs_bid) / p.tick + 1e-9) * p.tick;
  double ask = std::ceil((reservation + hs_ask) / p.tick - 1e-9) * p.tick;
  bid = std::min(bid, best_ask - p.tick);
  ask = std::max(ask, best_bid + p.tick);
  // 5) sizes shrink linearly on the risk-increasing side — integer math only
  //    (double 1.0-0.8 = 0.19999… would floor 100 shares down to 0)
  const int64_t q_long = std::clamp<int64_t>(inv, 0, p.inv_limit);
  const int64_t q_short = std::clamp<int64_t>(-inv, 0, p.inv_limit);
  auto lots = [&](int64_t room) { return (p.base_size * room / p.inv_limit) / p.lot * p.lot; };
  QuotePair q;
  q.bid_px = to_fixed(bid); q.ask_px = to_fixed(ask);
  q.bid_qty = lots(p.inv_limit - q_long);
  q.ask_qty = lots(p.inv_limit - q_short);
  q.bid_on = q.bid_qty > 0; q.ask_on = q.ask_qty > 0;
  return q;
}

}  // namespace hft
