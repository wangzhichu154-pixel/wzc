// A-share (SSE/SZSE) specific reference components for
// docs/design/ashare-huatai-market-making-system-design.md — NOT production code.
//
// Covered: daily reference data, price-limit-bounded ladder book with a 3-level bitmap,
// L2 tick-by-tick book reconstruction with SZSE vs SSE semantics, T+1 sellable tracking,
// cash freezing, fee / break-even model, price cage / lot / limit checks, regulatory
// message budgets (program-trading rules), cancel policy, A-share pre-trade check,
// T+0-around-base quote constraints.
#pragma once
#include "oms.hpp"   // SendResult, ExecSinkProbe, OrderRequest (via risk.hpp)

#include <cassert>

namespace hft::ashare {

// ───────────────────────── reference data (loaded pre-open, immutable intraday) ─────────────────────────
enum class Exchange : uint8_t { SSE, SZSE };
enum class Board : uint8_t { Main, Star, ChiNext, Bse };
enum class InstrType : uint8_t { Stock, Etf, ConvBond };

struct SecurityRef {
  uint32_t  sec_id;          // dense internal id == book slot
  Exchange  exch;
  Board     board;
  InstrType type;
  bool      t0;              // same-day round trip allowed (convertible bonds, some ETFs)
  bool      stamp_duty;      // stocks: seller pays stamp duty
  Price     tick;            // 1e-4 CNY: stock 0.01 -> 100, ETF/CB 0.001 -> 10
  Price     prev_close;
  Price     limit_up;        // 0 => no price limit today (e.g. first days after listing)
  Price     limit_down;
  Qty       buy_min, buy_step;    // main board 100/100, STAR 200/1, CB 10/10
  Qty       sell_lot;             // round lot for sells; odd remainder must go in one order
  int32_t   cage_bps;             // 200 => 2 % (0 => no cage)
  int32_t   cage_ticks;           // main board: 10 ticks alternative; STAR/ChiNext: 0
};

// ───────────────────────── 3-level occupancy bitmap (up to 262,144 levels) ─────────────────────────
class HierBitmap {
 public:
  void init(uint32_t n) {
    l0_.assign((n + 63) / 64, 0);
    l1_.assign((l0_.size() + 63) / 64, 0);
    l2_ = 0;
    assert(l1_.size() <= 64 && "ladder too large for 3-level bitmap");
  }
  void set(uint32_t i) noexcept {
    const uint32_t w0 = i >> 6, w1 = w0 >> 6;
    l0_[w0] |= 1ull << (i & 63);
    l1_[w1] |= 1ull << (w0 & 63);
    l2_ |= 1ull << w1;
  }
  void clear(uint32_t i) noexcept {
    const uint32_t w0 = i >> 6, w1 = w0 >> 6;
    l0_[w0] &= ~(1ull << (i & 63));
    if (l0_[w0]) return;
    l1_[w1] &= ~(1ull << (w0 & 63));
    if (!l1_[w1]) l2_ &= ~(1ull << w1);
  }
  int highest() const noexcept {
    if (!l2_) return -1;
    const int w1 = 63 - std::countl_zero(l2_);
    const int w0 = (w1 << 6) + 63 - std::countl_zero(l1_[w1]);
    return (w0 << 6) + 63 - std::countl_zero(l0_[w0]);
  }
  int lowest() const noexcept {
    if (!l2_) return -1;
    const int w1 = std::countr_zero(l2_);
    const int w0 = (w1 << 6) + std::countr_zero(l1_[w1]);
    return (w0 << 6) + std::countr_zero(l0_[w0]);
  }
  // highest set index strictly below i (bid depth iteration), -1 if none
  int prev_below(uint32_t i) const noexcept {
    if (i == 0) return -1;
    --i;
    uint32_t w0 = i >> 6;
    uint64_t m = l0_[w0] & (~0ull >> (63 - (i & 63)));
    if (m) return int((w0 << 6) + 63 - std::countl_zero(m));
    if (w0 == 0) return -1;
    uint32_t w1 = w0 >> 6;
    uint64_t m1 = (w0 & 63) ? (l1_[w1] & (~0ull >> (64 - (w0 & 63)))) : 0;
    if (!m1) {
      const uint64_t m2 = w1 ? (l2_ & (~0ull >> (64 - w1))) : 0;
      if (!m2) return -1;
      w1 = 63 - std::countl_zero(m2);
      m1 = l1_[w1];
    }
    w0 = (w1 << 6) + 63 - std::countl_zero(m1);
    return int((w0 << 6) + 63 - std::countl_zero(l0_[w0]));
  }
  // lowest set index strictly above i (ask depth iteration), -1 if none
  int next_above(uint32_t i) const noexcept {
    ++i;
    uint32_t w0 = i >> 6;
    if (w0 >= l0_.size()) return -1;
    uint64_t m = l0_[w0] & (~0ull << (i & 63));
    if (m) return int((w0 << 6) + std::countr_zero(m));
    uint32_t w1 = w0 >> 6;
    uint64_t m1 = ((w0 & 63) == 63) ? 0 : (l1_[w1] & (~0ull << ((w0 & 63) + 1)));
    if (!m1) {
      const uint64_t m2 = (w1 == 63) ? 0 : (l2_ & (~0ull << (w1 + 1)));
      if (!m2) return -1;
      w1 = std::countr_zero(m2);
      m1 = l1_[w1];
    }
    w0 = (w1 << 6) + std::countr_zero(m1);
    return int((w0 << 6) + std::countr_zero(l0_[w0]));
  }
 private:
  std::vector<uint64_t> l0_, l1_;
  uint64_t l2_ = 0;
};

// ───────────────────────── order book bounded by the daily price limits ─────────────────────────
// With price limits the full tradable range [limit_down, limit_up] is known pre-open, so the
// ladder covers it completely: O(1) price->level, no re-centering, no overflow path.
struct AOrder {               // 32 bytes
  uint64_t key;               // (channel << 40) | per-channel order sequence
  uint32_t px;                // 1e-4 CNY
  uint32_t qty;               // remaining shares on the book
  uint32_t prev, next;        // intrusive FIFO within the level
  uint32_t level;
  uint16_t book;
  Side     side;
  uint8_t  pad_;
};
static_assert(sizeof(AOrder) == 32);

struct ALevel { uint64_t qty = 0; uint32_t count = 0, head = kNil, tail = kNil, pad_ = 0; };

class AshareBook {
 public:
  void init(const SecurityRef& r, uint32_t no_limit_half_window_ticks = 20'000) {
    tick_ = uint32_t(r.tick);
    if (r.limit_up > 0) {
      lo_ = uint32_t(r.limit_down / r.tick);
      n_ = uint32_t((r.limit_up - r.limit_down) / r.tick) + 1;
    } else {  // no price limit: window around reference price; outside -> rejected (cold, alarm)
      const uint32_t ref = uint32_t(r.prev_close / r.tick);
      lo_ = ref > no_limit_half_window_ticks ? ref - no_limit_half_window_ticks : 0;
      n_ = 2 * no_limit_half_window_ticks + 1;
    }
    for (auto* s : {&bid_, &ask_}) { s->lv.assign(n_, ALevel{}); s->bits.init(n_); s->best = -1; }
  }
  bool index_of(uint32_t px, uint32_t& idx) const noexcept {
    const uint32_t t = px / tick_;
    if (t * tick_ != px || t < lo_ || t - lo_ >= n_) [[unlikely]] return false;
    idx = t - lo_;
    return true;
  }
  uint32_t px_at(uint32_t idx) const noexcept { return (lo_ + idx) * tick_; }
  uint32_t levels() const noexcept { return n_; }

  struct SideBook { std::vector<ALevel> lv; HierBitmap bits; int best = -1; };
  SideBook& side(Side s) noexcept { return s == Side::Buy ? bid_ : ask_; }
  const SideBook& side(Side s) const noexcept { return s == Side::Buy ? bid_ : ask_; }

  uint32_t best_px(Side s) const noexcept { const auto& b = side(s); return b.best < 0 ? 0 : px_at(uint32_t(b.best)); }
  uint64_t best_qty(Side s) const noexcept { const auto& b = side(s); return b.best < 0 ? 0 : b.lv[uint32_t(b.best)].qty; }

  bool link_back(std::vector<AOrder>& pool, uint32_t oi) noexcept {
    AOrder& o = pool[oi];
    SideBook& sb = side(o.side);
    ALevel& l = sb.lv[o.level];
    o.prev = l.tail; o.next = kNil;
    if (l.tail != kNil) pool[l.tail].next = oi; else l.head = oi;
    l.tail = oi;
    l.qty += o.qty;
    if (l.count++ == 0) {
      sb.bits.set(o.level);
      const bool better = sb.best < 0 || (o.side == Side::Buy ? int(o.level) > sb.best : int(o.level) < sb.best);
      if (better) sb.best = int(o.level);
    }
    return int(o.level) == sb.best;
  }
  bool unlink(std::vector<AOrder>& pool, uint32_t oi) noexcept {
    AOrder& o = pool[oi];
    SideBook& sb = side(o.side);
    ALevel& l = sb.lv[o.level];
    if (o.prev != kNil) pool[o.prev].next = o.next; else l.head = o.next;
    if (o.next != kNil) pool[o.next].prev = o.prev; else l.tail = o.prev;
    l.qty -= o.qty;
    const bool at_best = int(o.level) == sb.best;
    if (--l.count == 0) {
      sb.bits.clear(o.level);
      if (at_best) sb.best = o.side == Side::Buy ? sb.bits.highest() : sb.bits.lowest();
    }
    return at_best;
  }
  bool reduce(std::vector<AOrder>& pool, uint32_t oi, uint32_t q) noexcept {
    AOrder& o = pool[oi];
    SideBook& sb = side(o.side);
    sb.lv[o.level].qty -= q;
    o.qty -= q;
    return int(o.level) == sb.best;
  }
  // Top-n levels (for features and for the 3-second L2 snapshot cross-check).
  int depth(Side s, int n, uint32_t* px, uint64_t* qty) const noexcept {
    const SideBook& sb = side(s);
    int k = 0;
    for (int i = sb.best; i >= 0 && k < n; ++k) {
      px[k] = px_at(uint32_t(i)); qty[k] = sb.lv[uint32_t(i)].qty;
      i = s == Side::Buy ? sb.bits.prev_below(uint32_t(i)) : sb.bits.next_above(uint32_t(i));
    }
    return k;
  }
 private:
  SideBook bid_, ask_;
  uint32_t lo_ = 0, n_ = 0, tick_ = 100;
};

// ───────────────────────── normalized L2 tick-by-tick event ─────────────────────────
// Produced by per-source decoders (SZSE Binary/STEP, SSE STEP/Binary, broker data SDK such as
// INSIGHT). Field semantics differ by exchange; see AshareBookBuilder for how each is applied.
enum class L2Type : uint8_t { Order, Trade, Cancel };
enum class OrdKind : uint8_t { Limit, Market, BestOwn };   // SZSE OrdType '2' / '1' / 'U'
enum class Semantics : uint8_t { Szse, Sse };

inline constexpr uint64_t make_key(uint16_t channel, uint64_t seq) noexcept { return (uint64_t(channel) << 40) | seq; }

struct alignas(kCacheLine) L2Event {
  uint64_t seq;               // per-channel sequence (SZSE ApplSeqNum, SSE BizIndex in the merged feed)
  uint64_t exch_ts;
  uint64_t rx_ts;
  uint64_t key;               // Order / Cancel: order key
  uint64_t bid_key, ask_key;  // Trade: both sides
  uint32_t px;                // 1e-4 CNY (0 for market orders)
  uint32_t qty;               // Order: SZSE original qty / SSE resting remainder; Cancel: cancelled qty
  uint16_t channel;
  uint16_t book;              // dense security slot
  L2Type   type;
  Side     side;              // Order side; Trade: aggressor side (SSE BSFlag, SZSE inferred)
  OrdKind  kind;
  Semantics sem;
};
static_assert(sizeof(L2Event) == 64);

// ───────────────────────── L2 book reconstruction ─────────────────────────
// SZSE: every order is published with its ORIGINAL quantity, including aggressive ones; trades
//       reference both orders; cancels appear as trade records with ExecType "cancel"; IOC
//       remainders are cancelled explicitly; 对手方最优 remainder rests at the matched price.
//       -> an incoming crossing order is held as a per-channel "pending aggressor" until the
//          next unrelated message, then its remainder (if any) is inserted.
// SSE:  only the RESTING remainder of an order is published (after its immediate matching);
//       trades may reference an aggressor that never reaches the book -> ignore that side.
class AshareBookBuilder {
 public:
  static constexpr uint16_t kNoBook = 0xFFFF;
  static constexpr int kMaxChannels = 32;

  AshareBookBuilder(uint32_t max_orders, int log2_map, uint16_t max_books)
      : pool_(max_orders), free_(max_orders), top_(max_orders), map_(log2_map), books_(max_books) {
    for (uint32_t i = 0; i < max_orders; ++i) free_[i] = max_orders - 1 - i;
    chan_slot_.fill(0xFF);
  }
  void add_security(const SecurityRef& r) { books_[r.sec_id].init(r); }
  void add_channel(uint16_t channel) { chan_slot_[channel] = nchan_++; }
  AshareBook& book(uint16_t b) noexcept { return books_[b]; }

  struct Stats { uint64_t unknown_ref = 0, sse_aggressor_absent = 0, off_ladder = 0, crossed_after_flush = 0, pool_exhausted = 0, unknown_channel = 0; };
  const Stats& stats() const noexcept { return st_; }
  uint32_t live_orders() const noexcept { return uint32_t(pool_.size()) - top_; }

  // Returns true when the top of book of ev.book (or of a flushed pending order's book) changed.
  bool apply(const L2Event& ev) noexcept {
    const uint8_t cs = chan_slot_[ev.channel];
    if (cs >= kMaxChannels) [[unlikely]] { ++st_.unknown_channel; return false; }   // unregistered channel
    Pending& p = pend_[cs];
    bool changed = false;
    if (p.active && !related(p, ev)) changed |= flush(p);
    switch (ev.type) {
      case L2Type::Order: return on_order(ev, p) | changed;
      case L2Type::Trade: {
        changed |= fill_side(p, ev.bid_key, ev.qty, ev.px, ev.sem);
        changed |= fill_side(p, ev.ask_key, ev.qty, ev.px, ev.sem);
        return changed;
      }
      case L2Type::Cancel:
        if (p.active && ev.key == p.key) { p.active = false; return changed; }  // IOC remainder cancelled
        return reduce_or_remove(ev.key, ev.sem == Semantics::Sse ? 0xFFFF'FFFFu : ev.qty) | changed;
    }
    return changed;
  }
  // A SZSE matching burst (order + its trades / IOC cancel) is contiguous in the channel sequence, but it
  // may span packets. Call this from a timer when a channel has been quiet for T us; until then the
  // book of a symbol with a pending aggressor is "in transition" and the strategy must not quote on it.
  bool has_pending(uint16_t channel) const noexcept { const uint8_t cs = chan_slot_[channel]; return cs < kMaxChannels && pend_[cs].active; }
  bool flush_all() noexcept {
    bool c = false;
    for (int i = 0; i < nchan_; ++i) if (pend_[i].active) c |= flush(pend_[i]);
    return c;
  }

 private:
  struct Pending {
    uint64_t key = 0; uint32_t limit_px = 0, remaining = 0, last_fill_px = 0;
    uint16_t book = 0; Side side = Side::Buy; OrdKind kind = OrdKind::Limit; bool active = false;
  };
  static bool related(const Pending& p, const L2Event& ev) noexcept {
    return (ev.type == L2Type::Trade && (ev.bid_key == p.key || ev.ask_key == p.key)) ||
           (ev.type == L2Type::Cancel && ev.key == p.key);
  }
  bool on_order(const L2Event& ev, Pending& p) noexcept {
    AshareBook& b = books_[ev.book];
    if (ev.sem == Semantics::Sse) return insert(ev.key, ev.book, ev.side, ev.px, ev.qty);
    uint32_t px = ev.px;
    if (ev.kind == OrdKind::BestOwn) {           // 本方最优: priced at own-side best on entry
      px = b.best_px(ev.side);
      if (!px) return false;                     // exchange rejects it when own side is empty
    }
    const uint32_t opp = b.best_px(ev.side == Side::Buy ? Side::Sell : Side::Buy);
    const bool crossing = ev.kind == OrdKind::Market ||
                          (opp && (ev.side == Side::Buy ? px >= opp : px <= opp));
    if (crossing) {
      p = Pending{ev.key, px, ev.qty, 0, ev.book, ev.side, ev.kind, true};
      return false;
    }
    return insert(ev.key, ev.book, ev.side, px, ev.qty);
  }
  bool fill_side(Pending& p, uint64_t key, uint32_t q, uint32_t px, Semantics sem) noexcept {
    if (!key) return false;
    if (p.active && key == p.key) {
      p.remaining = q >= p.remaining ? 0 : p.remaining - q;
      p.last_fill_px = px;
      return false;
    }
    const uint32_t oi = map_.find(key);
    if (oi == kNil) {
      if (sem == Semantics::Sse) ++st_.sse_aggressor_absent; else ++st_.unknown_ref;
      return false;
    }
    return reduce_at(oi, q);
  }
  bool flush(Pending& p) noexcept {
    p.active = false;
    if (!p.remaining) return false;
    // Limit: rests at its limit; 对手方最优 market: rests at the (single) matched price.
    const uint32_t px = p.kind == OrdKind::Market ? p.last_fill_px : p.limit_px;
    if (!px) return false;                       // market order with no fill: nothing rests
    AshareBook& b = books_[p.book];
    const uint32_t opp = b.best_px(p.side == Side::Buy ? Side::Sell : Side::Buy);
    if (opp && (p.side == Side::Buy ? px >= opp : px <= opp)) ++st_.crossed_after_flush;  // data issue -> alarm
    return insert(p.key, p.book, p.side, px, p.remaining);
  }
  bool insert(uint64_t key, uint16_t book, Side s, uint32_t px, uint32_t qty) noexcept {
    AshareBook& b = books_[book];
    uint32_t idx;
    if (!b.index_of(px, idx)) [[unlikely]] { ++st_.off_ladder; return false; }
    if (!top_) [[unlikely]] { ++st_.pool_exhausted; return false; }
    const uint32_t oi = free_[--top_];
    AOrder& o = pool_[oi];
    o.key = key; o.px = px; o.qty = qty; o.level = idx; o.book = book; o.side = s;
    map_.insert(key, oi);
    return b.link_back(pool_, oi);
  }
  bool reduce_or_remove(uint64_t key, uint32_t q) noexcept {
    const uint32_t oi = map_.find(key);
    if (oi == kNil) [[unlikely]] { ++st_.unknown_ref; return false; }
    return reduce_at(oi, q);
  }
  bool reduce_at(uint32_t oi, uint32_t q) noexcept {
    AOrder& o = pool_[oi];
    AshareBook& b = books_[o.book];
    if (q < o.qty) return b.reduce(pool_, oi, q);
    const bool c = b.unlink(pool_, oi);
    map_.erase(o.key);
    free_[top_++] = oi;
    return c;
  }

  std::vector<AOrder> pool_;
  std::vector<uint32_t> free_;
  uint32_t top_;
  OrderMap map_;
  std::vector<AshareBook> books_;
  std::array<Pending, kMaxChannels> pend_{};
  std::array<uint8_t, 65536> chan_slot_{};
  int nchan_ = 0;
  Stats st_;
};

// ───────────────────────── T+1 position & cash ─────────────────────────
struct T1Position {
  Qty  yd_qty = 0;          // settled position at the open (sellable base for T+1 instruments)
  Qty  bought_today = 0;    // filled buys today (NOT sellable today unless t0)
  Qty  sold_today = 0;      // filled sells today
  Qty  frozen_sell = 0;     // leaves of live sell orders (counter freezes these shares)
  Qty  open_buy = 0;        // leaves of live buy orders
  bool t0 = false;
  Qty position() const noexcept { return yd_qty + bought_today - sold_today; }
  Qty sellable() const noexcept {
    const Qty base = t0 ? position() : yd_qty - sold_today;
    return base - frozen_sell;
  }
  Qty net_bought_today() const noexcept { return bought_today - sold_today; }  // > 0 adds overnight risk
};

struct CashAccount {        // all 1e-4 CNY
  int64_t available = 0;    // usable for new buys (same-day sell proceeds are reusable for buys)
  int64_t frozen = 0;       // reserved by live buy orders (notional + estimated fees)
};

// ───────────────────────── fees & break-even ─────────────────────────
// Rates in units of 1e-8 (0.01 % == 10'000). Values are configuration, loaded daily and versioned.
struct FeeSchedule {
  int64_t commission_rate = 10'000;  // e.g. 0.01 % (万1) — negotiated with the broker
  int64_t commission_min = 0;        // per order, 1e-4 CNY (a 5-CNY minimum would kill small orders)
  int64_t stamp_duty_rate = 50'000;  // 0.05 % sell side, stocks only (halved from 0.1 % on 2023-08-28)
  int64_t transfer_rate = 1'000;     // 0.001 % both sides (verify current CSDC schedule)
  int64_t exchange_rate = 3'410;     // 经手费 0.00341 % both sides (since 2023-08-28)
  int64_t regulator_rate = 2'000;    // 证管费 0.002 % both sides
  bool    exch_fees_in_commission = true;  // many brokers bundle 经手费/证管费 into commission
};

inline int64_t ceil_fen(int64_t v) noexcept { return (v + 99) / 100 * 100; }  // round up to 0.01 CNY

inline int64_t fee_for_fill(const FeeSchedule& f, const SecurityRef& r, Side s, int64_t notional) noexcept {
  int64_t fee = std::max(notional * f.commission_rate / 100'000'000, f.commission_min);
  if (r.type == InstrType::Stock) fee += notional * f.transfer_rate / 100'000'000;
  if (!f.exch_fees_in_commission) fee += notional * (f.exchange_rate + f.regulator_rate) / 100'000'000;
  if (s == Side::Sell && r.stamp_duty) fee += notional * f.stamp_duty_rate / 100'000'000;
  return ceil_fen(fee);
}

// Smallest number of ticks between buy and sell price that covers a full round trip of qty shares.
inline int64_t breakeven_ticks(const FeeSchedule& f, const SecurityRef& r, Price px, Qty qty) noexcept {
  for (int64_t k = 1; k < 1000; ++k) {
    const int64_t buy_n = px * qty, sell_n = (px + k * r.tick) * qty;
    const int64_t cost = fee_for_fill(f, r, Side::Buy, buy_n) + fee_for_fill(f, r, Side::Sell, sell_n);
    if (sell_n - buy_n > cost) return k;
  }
  return -1;
}

// ───────────────────────── exchange order-validity checks ─────────────────────────
inline bool price_limit_ok(const SecurityRef& r, Price px) noexcept {
  return r.limit_up == 0 || (px >= r.limit_down && px <= r.limit_up);
}
inline bool on_tick(const SecurityRef& r, Price px) noexcept { return px > 0 && px % r.tick == 0; }

// Price cage (价格笼子). Buy: px <= max(bench*(1+bps), bench + n ticks); sell: px >= min(bench*(1-bps), bench - n ticks).
// bench = best counter-side price (buy: best ask; sell: best bid), fallbacks per exchange rules.
inline bool price_cage_ok(const SecurityRef& r, Side s, Price px, Price bench) noexcept {
  if (r.cage_bps == 0 || bench <= 0) return true;
  if (s == Side::Buy) {
    const Price cap = std::max<Price>(bench * (10'000 + r.cage_bps) / 10'000, bench + r.cage_ticks * r.tick);
    return px <= cap;
  }
  const Price floor = std::min<Price>((bench * (10'000 - r.cage_bps) + 9'999) / 10'000, bench - r.cage_ticks * r.tick);
  return px >= floor;
}

inline bool lot_ok(const SecurityRef& r, Side s, Qty q, Qty sellable) noexcept {
  if (q <= 0) return false;
  if (s == Side::Buy) return q >= r.buy_min && (q - r.buy_min) % r.buy_step == 0;
  if (q % r.sell_lot == 0) return true;
  // the odd remainder (< one lot) must be sold in a single order: allow q whose odd part == sellable's odd part
  return q <= sellable && q % r.sell_lot == sellable % r.sell_lot;
}

// ───────────────────────── regulatory message budgets ─────────────────────────
// Program-trading rules (SSE/SZSE/BSE, effective 2025-07-07): an account reaching >= 300 orders+cancels
// in one second or >= 20,000 in one day is classified as high-frequency trading (extra reporting,
// stricter supervision, differentiated fees). The budget keeps the account below a configured cap
// and paces usage over the trading session so the cap is not exhausted early.
class DailyMessageBudget {
 public:
  DailyMessageBudget(uint32_t cap, uint32_t emergency_reserve, uint32_t burst, uint64_t session_ns) noexcept
      : cap_(cap), reserve_(emergency_reserve), burst_(burst), session_ns_(session_ns) {}
  // elapsed_ns: continuous-trading time elapsed today (lunch break excluded)
  uint32_t allowance(uint64_t elapsed_ns) const noexcept {
    const uint64_t pace = uint64_t(cap_ - reserve_ - burst_) * std::min(elapsed_ns, session_ns_) / session_ns_;
    return burst_ + uint32_t(pace);
  }
  bool can_spend(uint64_t elapsed_ns, bool emergency) const noexcept {
    if (emergency) return used_ < cap_;             // cancels during kill / risk reduction may use the reserve
    return used_ < allowance(elapsed_ns);
  }
  void spend() noexcept { ++used_; }
  uint32_t used() const noexcept { return used_; }
 private:
  uint32_t cap_, reserve_, burst_;
  uint64_t session_ns_;
  uint32_t used_ = 0;
};

// Avoid "frequent instantaneous cancellation" (频繁瞬时撤单: repeated cancels within 1 s of entry plus a
// high daily cancel ratio). Exact exchange thresholds must be taken from the rules; values are config.
class CancelPolicy {
 public:
  CancelPolicy(uint64_t min_rest_ns, uint32_t max_instant_per_day, uint32_t max_cancel_ratio_pct,
               uint32_t ratio_min_sample) noexcept
      : min_rest_ns_(min_rest_ns), max_instant_(max_instant_per_day), ratio_pct_(max_cancel_ratio_pct),
        min_sample_(ratio_min_sample) {}
  enum class Decision : uint8_t { Allow, Defer, Deny };
  Decision check(uint64_t now, uint64_t entered_ts, bool emergency) const noexcept {
    if (emergency) return Decision::Allow;                       // risk reduction always wins
    if (now - entered_ts < min_rest_ns_) return instant_ < max_instant_ ? Decision::Allow : Decision::Defer;
    if (orders_ >= min_sample_ && uint64_t(cancels_ + 1) * 100 > uint64_t(ratio_pct_) * orders_) return Decision::Deny;
    return Decision::Allow;
  }
  void on_order() noexcept { ++orders_; }
  void on_cancel(uint64_t now, uint64_t entered_ts) noexcept { ++cancels_; if (now - entered_ts < min_rest_ns_) ++instant_; }
  uint32_t instant() const noexcept { return instant_; }
 private:
  uint64_t min_rest_ns_;
  uint32_t max_instant_, ratio_pct_, min_sample_;
  uint32_t orders_ = 0, cancels_ = 0, instant_ = 0;
};

// ───────────────────────── A-share pre-trade check ─────────────────────────
enum class SessionPhase : uint8_t { PreOpen, OpenAuction, Continuous, LunchBreak, CloseAuction, Closed };

struct ASymbolState {           // per symbol, owned by the shard thread
  const SecurityRef* ref;
  T1Position pos;
  Price best_bid, best_ask;     // venue book
  Price own_best_bid, own_best_ask;   // our own live quotes (0 = none) — self-trade prevention
  Qty   max_position;           // absolute position cap (shares)
  Qty   max_net_buy_today;      // cap on today's net buys (unsellable overnight risk for T+1)
  uint32_t last_md_ts32;
  uint16_t flags;               // kSymHalted / kSymBookInvalid ...
  bool  allow_quote_at_limit;   // default false: no resting orders at limit-up/down price
};

struct AAccountState {
  const std::atomic<uint64_t>* global_block;
  CashAccount cash;
  FeeSchedule fees;
  int64_t net_pnl, daily_loss_limit;
  SessionPhase phase;
  uint32_t md_stale_ns;
  uint64_t session_elapsed_ns;
  DailyMessageBudget* budget;
  SlidingWindowLimiter<100>* per_second;   // far below the 300/s HFT line
};

struct AOrderReq { Price px; Qty qty; Side side; bool cancel; bool emergency; bool passive; };

enum class AVerdict : uint8_t {
  Ok, KillSwitch, Blocked, SessionClosed, Halted, BookInvalid, Stale, OffTick, PriceLimit, AtLimitPrice,
  PriceCage, BadLot, WouldCross, SelfTrade, InsufficientSellable, InsufficientCash, MaxPosition,
  OvernightCap, ReduceOnly, DailyLoss, PerSecond, DailyBudget
};

[[nodiscard]] inline AVerdict
ashare_pre_trade_check(const AOrderReq& o, const ASymbolState& s, const AAccountState& a, uint64_t now) noexcept {
  const uint64_t blk = a.global_block->load(std::memory_order_acquire);
  if (o.cancel) [[unlikely]] {   // cancels: only the message budgets apply (emergency may use the reserve)
    if (!a.per_second->can_acquire(now)) return AVerdict::PerSecond;
    return a.budget->can_spend(a.session_elapsed_ns, o.emergency) ? AVerdict::Ok : AVerdict::DailyBudget;
  }
  constexpr uint64_t kHard = kBlkKill | kBlkStrategyOff | kBlkGatewayDown | kBlkMdGap | kBlkClockBad |
                             kBlkLatency | kBlkRejectRate | kBlkLogBackpressure;
  if (blk & kHard) [[unlikely]] return (blk & kBlkKill) ? AVerdict::KillSwitch : AVerdict::Blocked;
  if (a.phase != SessionPhase::Continuous) [[unlikely]] return AVerdict::SessionClosed;
  if (s.flags & (kSymHalted | kSymBookInvalid)) [[unlikely]]
    return (s.flags & kSymHalted) ? AVerdict::Halted : AVerdict::BookInvalid;
  if (uint32_t(now) - s.last_md_ts32 > a.md_stale_ns) [[unlikely]] return AVerdict::Stale;

  const SecurityRef& r = *s.ref;
  const bool buy = o.side == Side::Buy;
  if (!on_tick(r, o.px)) [[unlikely]] return AVerdict::OffTick;
  if (!price_limit_ok(r, o.px)) [[unlikely]] return AVerdict::PriceLimit;
  if (r.limit_up && !s.allow_quote_at_limit && (o.px == r.limit_up || o.px == r.limit_down)) [[unlikely]]
    return AVerdict::AtLimitPrice;
  if (!price_cage_ok(r, o.side, o.px, buy ? s.best_ask : s.best_bid)) [[unlikely]] return AVerdict::PriceCage;
  if (!lot_ok(r, o.side, o.qty, s.pos.sellable())) [[unlikely]] return AVerdict::BadLot;
  if (o.passive && (buy ? (s.best_ask && o.px >= s.best_ask) : (s.best_bid && o.px <= s.best_bid))) [[unlikely]]
    return AVerdict::WouldCross;                                    // A-shares have no post-only order type
  if (buy ? (s.own_best_ask && o.px >= s.own_best_ask) : (s.own_best_bid && o.px <= s.own_best_bid)) [[unlikely]]
    return AVerdict::SelfTrade;                                     // never trade against ourselves (对倒)

  if (!buy) {
    if (o.qty > s.pos.sellable()) [[unlikely]] return AVerdict::InsufficientSellable;  // T+1, no short
  } else {
    const int64_t notional = o.px * o.qty;
    if (notional + fee_for_fill(a.fees, r, Side::Buy, notional) > a.cash.available) [[unlikely]]
      return AVerdict::InsufficientCash;
    if (s.pos.position() + s.pos.open_buy + o.qty > s.max_position) [[unlikely]] return AVerdict::MaxPosition;
    if (!s.pos.t0 && s.pos.net_bought_today() + s.pos.open_buy + o.qty > s.max_net_buy_today) [[unlikely]]
      return AVerdict::OvernightCap;
    if (blk & kBlkReduceOnly) [[unlikely]] return AVerdict::ReduceOnly;   // buys add risk (no shorts exist)
    if (a.net_pnl <= -a.daily_loss_limit) [[unlikely]] return AVerdict::DailyLoss;
  }
  if (!a.per_second->can_acquire(now)) [[unlikely]] return AVerdict::PerSecond;
  if (!a.budget->can_spend(a.session_elapsed_ns, false)) [[unlikely]] return AVerdict::DailyBudget;
  return AVerdict::Ok;
}

// ───────────────────────── T+0 around a base position ─────────────────────────
// Stocks are T+1: shares bought today cannot be sold today. Intraday two-sided quoting therefore
// rotates around a base inventory held overnight: sells consume yesterday's shares, buys rebuild them.
struct T0Limits { Qty max_bid_qty, max_ask_qty; bool bid_on, ask_on; };

inline T0Limits t0_quote_limits(const T1Position& p, Qty base_target, Qty band, Qty max_net_buy_today,
                                bool late_session) noexcept {
  const Qty dev = p.position() - base_target;      // > 0: above base (bought more than sold)
  T0Limits l{};
  l.max_ask_qty = std::max<Qty>(0, std::min(p.sellable(), band + dev - p.frozen_sell));  // stay >= base - band
  const Qty buy_room_band = std::max<Qty>(0, band - dev - p.open_buy);
  const Qty buy_room_overnight = p.t0 ? buy_room_band
                                      : std::max<Qty>(0, max_net_buy_today - p.net_bought_today() - p.open_buy);
  l.max_bid_qty = std::min(buy_room_band, buy_room_overnight);
  if (dev <= -band) l.max_ask_qty = 0;             // sold too much of the base: only buy back
  if (late_session) {                              // converge to base before the close, add no new risk
    if (dev >= 0) l.max_bid_qty = 0;
    if (dev <= 0) l.max_ask_qty = 0;
    l.max_ask_qty = std::min(l.max_ask_qty, std::max<Qty>(0, dev));
    l.max_bid_qty = std::min(l.max_bid_qty, std::max<Qty>(0, -dev));
  }
  l.bid_on = l.max_bid_qty > 0;
  l.ask_on = l.max_ask_qty > 0;
  return l;
}

// ───────────────────────── execution gateway (A-share: no replace) ─────────────────────────
template <class G>
concept AshareExecutionGateway = requires(G g, const OrderRequest& r, ExecSinkProbe& sink) {
  { g.send_new(r) } noexcept -> std::same_as<SendResult>;
  { g.send_cancel(r) } noexcept -> std::same_as<SendResult>;
  { g.cancel_all() } noexcept -> std::same_as<SendResult>;   // batch cancel if the API offers it, else iterate
  { g.poll(sink) } noexcept -> std::same_as<int>;
  { g.healthy() } noexcept -> std::same_as<bool>;
};

class SimAshareGatewayStub {   // shape check only; the real simulator lives in broker/sim_ashare
 public:
  SendResult send_new(const OrderRequest&) noexcept { return SendResult::Ok; }
  SendResult send_cancel(const OrderRequest&) noexcept { return SendResult::Ok; }
  SendResult cancel_all() noexcept { return SendResult::Ok; }
  template <class Sink> int poll(Sink&) noexcept { return 0; }
  bool healthy() noexcept { return true; }
};
static_assert(AshareExecutionGateway<SimAshareGatewayStub>);
static_assert(!AshareExecutionGateway<int>);

}  // namespace hft::ashare
