// Reference prototype of hot-path components used in the design doc.
// Purpose: make sure the code shown in the doc compiles, is correct, and to
// get order-of-magnitude numbers. NOT production code.
#pragma once
#include <algorithm>
#include <array>
#include <atomic>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <new>
#include <type_traits>
#include <vector>

namespace hft {

// ───────────────────────── basic types ─────────────────────────
using Price    = int64_t;   // fixed point, 1e-4 USD (ITCH native scale)
using Qty      = int64_t;   // shares, signed for positions
using SymbolId = uint16_t;  // == ITCH Stock Locate (assigned per trading day)
enum class Side : uint8_t { Buy = 0, Sell = 1 };
inline constexpr std::size_t kCacheLine = 64;
inline constexpr uint32_t kNil = 0xFFFF'FFFFu;

// ───────────────────────── big-endian loads ─────────────────────────
inline uint16_t be16(const uint8_t* p) noexcept { uint16_t v; std::memcpy(&v, p, 2); return __builtin_bswap16(v); }
inline uint32_t be32(const uint8_t* p) noexcept { uint32_t v; std::memcpy(&v, p, 4); return __builtin_bswap32(v); }
inline uint64_t be64(const uint8_t* p) noexcept { uint64_t v; std::memcpy(&v, p, 8); return __builtin_bswap64(v); }
inline uint64_t be48(const uint8_t* p) noexcept { return (uint64_t(be16(p)) << 32) | be32(p + 2); }

// ───────────────────────── MarketEvent ─────────────────────────
enum class MdType : uint8_t {
  Add, Execute, ExecuteWithPrice, Cancel, Delete, Replace,
  Trade, BrokenTrade, TradingAction, SystemEvent, Ignored
};

struct alignas(kCacheLine) MarketEvent {
  uint64_t seq;         // MoldUDP64 sequence number of the message
  uint64_t exch_ts_ns;  // ITCH timestamp: ns since midnight (ET)
  uint64_t rx_ts;       // local RX timestamp (HW timestamp or TSC)
  uint64_t ref;         // order ref (Replace: original ref)
  uint64_t aux;         // Replace: new ref | Execute/Trade/Broken: match number
  uint32_t px;          // price, 1e-4 USD
  uint32_t qty;         // shares
  SymbolId sym;         // stock locate
  MdType   type;
  Side     side;
  uint8_t  flag;        // Execute w/ price: printable; TradingAction: state char
};
static_assert(sizeof(MarketEvent) == 64);

// Decode one ITCH 5.0 message (already de-framed from MoldUDP64).
// Returns false for unknown / truncated messages (cold path).
inline bool decode_itch(const uint8_t* m, uint16_t len, uint64_t seq,
                        uint64_t rx_ts, MarketEvent& ev) noexcept {
  if (len < 11) [[unlikely]] return false;
  ev.seq = seq;
  ev.rx_ts = rx_ts;
  ev.sym = be16(m + 1);
  ev.exch_ts_ns = be48(m + 5);
  switch (m[0]) {
    case 'A':  // Add Order, 36 bytes
    case 'F':  // Add Order w/ MPID attribution, 40 bytes
      if (len < 36) [[unlikely]] return false;
      ev.type = MdType::Add;
      ev.ref  = be64(m + 11);
      ev.side = (m[19] == 'B') ? Side::Buy : Side::Sell;
      ev.qty  = be32(m + 20);
      ev.px   = be32(m + 32);
      return true;
    case 'E':  // Order Executed, 31 bytes
      if (len < 31) [[unlikely]] return false;
      ev.type = MdType::Execute;
      ev.ref = be64(m + 11); ev.qty = be32(m + 19); ev.aux = be64(m + 23);
      return true;
    case 'C':  // Order Executed With Price, 36 bytes
      if (len < 36) [[unlikely]] return false;
      ev.type = MdType::ExecuteWithPrice;
      ev.ref = be64(m + 11); ev.qty = be32(m + 19); ev.aux = be64(m + 23);
      ev.flag = m[31];  // 'Y' printable / 'N' non-printable
      ev.px = be32(m + 32);
      return true;
    case 'X':  // Order Cancel (partial), 23 bytes
      if (len < 23) [[unlikely]] return false;
      ev.type = MdType::Cancel; ev.ref = be64(m + 11); ev.qty = be32(m + 19);
      return true;
    case 'D':  // Order Delete, 19 bytes
      if (len < 19) [[unlikely]] return false;
      ev.type = MdType::Delete; ev.ref = be64(m + 11);
      return true;
    case 'U':  // Order Replace, 35 bytes
      if (len < 35) [[unlikely]] return false;
      ev.type = MdType::Replace;
      ev.ref = be64(m + 11); ev.aux = be64(m + 19);
      ev.qty = be32(m + 27); ev.px = be32(m + 31);
      return true;
    case 'P':  // Trade (non-cross, non-displayed liquidity), 44 bytes
      if (len < 44) [[unlikely]] return false;
      ev.type = MdType::Trade;
      ev.ref = be64(m + 11); ev.side = (m[19] == 'B') ? Side::Buy : Side::Sell;
      ev.qty = be32(m + 20); ev.px = be32(m + 32); ev.aux = be64(m + 36);
      return true;
    case 'B':  // Broken Trade, 19 bytes
      if (len < 19) [[unlikely]] return false;
      ev.type = MdType::BrokenTrade; ev.aux = be64(m + 11);
      return true;
    case 'H':  // Stock Trading Action, 25 bytes
      if (len < 25) [[unlikely]] return false;
      ev.type = MdType::TradingAction; ev.flag = m[19];  // H/P/Q/T
      return true;
    case 'S':
      if (len < 12) [[unlikely]] return false;
      ev.type = MdType::SystemEvent; ev.flag = m[11];
      return true;
    default:
      ev.type = MdType::Ignored;
      return true;
  }
}

// ───────────────────────── SPSC ring ─────────────────────────
template <typename T, std::size_t N>
class SpscRing {
  static_assert(std::has_single_bit(N), "N must be a power of two");
  static_assert(std::is_trivially_copyable_v<T>);
 public:
  bool try_push(const T& v) noexcept {
    const uint64_t h = head_.load(std::memory_order_relaxed);
    if (h - tail_cache_ == N) [[unlikely]] {
      tail_cache_ = tail_.load(std::memory_order_acquire);
      if (h - tail_cache_ == N) return false;  // full: caller decides (never block)
    }
    buf_[h & (N - 1)] = v;
    head_.store(h + 1, std::memory_order_release);
    return true;
  }
  bool try_pop(T& out) noexcept {
    const uint64_t t = tail_.load(std::memory_order_relaxed);
    if (t == head_cache_) [[unlikely]] {
      head_cache_ = head_.load(std::memory_order_acquire);
      if (t == head_cache_) return false;
    }
    out = buf_[t & (N - 1)];
    tail_.store(t + 1, std::memory_order_release);
    return true;
  }
 private:
  alignas(kCacheLine) std::atomic<uint64_t> head_{0};  // written by producer
  alignas(kCacheLine) uint64_t tail_cache_{0};         // producer-private
  alignas(kCacheLine) std::atomic<uint64_t> tail_{0};  // written by consumer
  alignas(kCacheLine) uint64_t head_cache_{0};         // consumer-private
  alignas(kCacheLine) std::array<T, N> buf_{};
};

// ───────────────────────── Order book ─────────────────────────
inline constexpr uint32_t kLadder = 4096;          // price levels per side (power of 2)
inline constexpr uint16_t kOverflowLevel = 0xFFFF; // order lives outside the ladder window

struct BookOrder {          // 32 bytes -> 2 per cache line
  uint64_t ref;             // ITCH order reference number (day-unique)
  uint32_t px;              // 1e-4 USD
  uint32_t qty;             // remaining displayed shares
  uint32_t prev, next;      // intrusive FIFO links (pool indices) within the level
  uint16_t level;           // ladder index or kOverflowLevel
  SymbolId sym;
  Side side;
  uint8_t pad_[3];
};
static_assert(sizeof(BookOrder) == 32);

struct PriceLevel {         // 24 bytes
  uint64_t qty = 0;         // aggregate displayed shares
  uint32_t count = 0;       // number of orders
  uint32_t head = kNil;     // oldest order (front of queue)
  uint32_t tail = kNil;     // newest order
  uint32_t pad_ = 0;
};

// Pre-allocated object pool with an index free-list (no heap after init).
class OrderPool {
 public:
  explicit OrderPool(uint32_t cap) : slots_(cap), free_(cap), top_(cap) {
    for (uint32_t i = 0; i < cap; ++i) free_[i] = cap - 1 - i;
  }
  uint32_t alloc() noexcept { return top_ ? free_[--top_] : kNil; }
  void release(uint32_t i) noexcept { free_[top_++] = i; }
  BookOrder& operator[](uint32_t i) noexcept { return slots_[i]; }
  const BookOrder& operator[](uint32_t i) const noexcept { return slots_[i]; }
  uint32_t in_use() const noexcept { return uint32_t(slots_.size()) - top_; }
 private:
  std::vector<BookOrder> slots_;
  std::vector<uint32_t> free_;
  uint32_t top_;
};

// Open-addressing hash map: ITCH order ref -> pool index.
// Linear probing, Fibonacci hashing, backward-shift deletion (no tombstones).
// Key 0 marks an empty slot (ITCH refs are never 0).
class OrderMap {
 public:
  explicit OrderMap(int log2cap)
      : t_(std::size_t{1} << log2cap), mask_((std::size_t{1} << log2cap) - 1), shift_(64 - log2cap) {}
  bool insert(uint64_t k, uint32_t v) noexcept {
    std::size_t i = home(k);
    while (t_[i].key) {
      if (t_[i].key == k) [[unlikely]] return false;
      i = (i + 1) & mask_;
    }
    t_[i] = {k, v};
    return true;
  }
  uint32_t find(uint64_t k) const noexcept {
    for (std::size_t i = home(k);; i = (i + 1) & mask_) {
      if (t_[i].key == k) return t_[i].val;
      if (!t_[i].key) return kNil;
    }
  }
  bool erase(uint64_t k) noexcept {
    std::size_t i = home(k);
    while (t_[i].key != k) {
      if (!t_[i].key) return false;
      i = (i + 1) & mask_;
    }
    for (std::size_t j = (i + 1) & mask_; t_[j].key; j = (j + 1) & mask_) {
      const std::size_t h = home(t_[j].key);
      const bool stays = (i <= j) ? (i < h && h <= j) : (i < h || h <= j);
      if (!stays) { t_[i] = t_[j]; i = j; }
    }
    t_[i].key = 0;
    return true;
  }
 private:
  struct Slot { uint64_t key = 0; uint32_t val = 0; uint32_t pad_ = 0; };
  std::size_t home(uint64_t k) const noexcept { return (k * 0x9E3779B97F4A7C15ull) >> shift_; }
  std::vector<Slot> t_;
  std::size_t mask_;
  int shift_;
};

// One side of the book: flat price ladder + 2-level occupancy bitmap.
class Ladder {
 public:
  PriceLevel& operator[](uint32_t i) noexcept { return lv_[i]; }
  const PriceLevel& operator[](uint32_t i) const noexcept { return lv_[i]; }
  void mark(uint32_t i) noexcept {
    words_[i >> 6] |= 1ull << (i & 63);
    summary_ |= 1ull << (i >> 6);
  }
  void unmark(uint32_t i) noexcept {
    uint64_t& w = words_[i >> 6];
    w &= ~(1ull << (i & 63));
    if (!w) summary_ &= ~(1ull << (i >> 6));
  }
  int highest() const noexcept {  // best bid index, -1 if empty
    if (!summary_) return -1;
    const int w = 63 - std::countl_zero(summary_);
    return (w << 6) + 63 - std::countl_zero(words_[w]);
  }
  int lowest() const noexcept {   // best ask index, -1 if empty
    if (!summary_) return -1;
    const int w = std::countr_zero(summary_);
    return (w << 6) + std::countr_zero(words_[w]);
  }
 private:
  std::array<PriceLevel, kLadder> lv_{};
  std::array<uint64_t, kLadder / 64> words_{};
  uint64_t summary_ = 0;
};

struct TopOfBook {
  uint32_t bid_px = 0, ask_px = 0;  // 0 = empty side
  uint64_t bid_qty = 0, ask_qty = 0;
  uint32_t bid_cnt = 0, ask_cnt = 0;
};

class OrderBook {
 public:
  void init(SymbolId sym, uint32_t ref_px, uint32_t tick) noexcept {
    sym_ = sym; tick_ = tick;
    const uint32_t t = ref_px / tick;
    base_ = t > kLadder / 2 ? t - kLadder / 2 : 0;
  }
  // price -> ladder index; false if off-grid or outside window (overflow path)
  bool index_of(uint32_t px, uint32_t& idx) const noexcept {
    const uint32_t t = px / tick_;
    if (t * tick_ != px || t < base_ || t - base_ >= kLadder) [[unlikely]] return false;
    idx = t - base_;
    return true;
  }
  uint32_t px_at(uint32_t idx) const noexcept { return (base_ + idx) * tick_; }
  Ladder& side(Side s) noexcept { return s == Side::Buy ? bids_ : asks_; }
  const Ladder& side(Side s) const noexcept { return s == Side::Buy ? bids_ : asks_; }

  TopOfBook top() const noexcept {
    TopOfBook t;
    if (best_bid_ >= 0) { const auto& l = bids_[best_bid_]; t.bid_px = px_at(best_bid_); t.bid_qty = l.qty; t.bid_cnt = l.count; }
    if (best_ask_ >= 0) { const auto& l = asks_[best_ask_]; t.ask_px = px_at(best_ask_); t.ask_qty = l.qty; t.ask_cnt = l.count; }
    return t;
  }
  int best_bid_idx() const noexcept { return best_bid_; }
  int best_ask_idx() const noexcept { return best_ask_; }

  // Level-level operations; return true if top-of-book changed.
  bool link_back(OrderPool& pool, uint32_t oi) noexcept {
    BookOrder& o = pool[oi];
    Ladder& lad = side(o.side);
    PriceLevel& l = lad[o.level];
    o.prev = l.tail; o.next = kNil;
    if (l.tail != kNil) pool[l.tail].next = oi; else l.head = oi;
    l.tail = oi;
    l.qty += o.qty;
    if (l.count++ == 0) {
      lad.mark(o.level);
      int& best = o.side == Side::Buy ? best_bid_ : best_ask_;
      const bool better = best < 0 || (o.side == Side::Buy ? int(o.level) > best : int(o.level) < best);
      if (better) best = o.level;
      return better || int(o.level) == best;
    }
    return int(o.level) == (o.side == Side::Buy ? best_bid_ : best_ask_);
  }
  bool unlink(OrderPool& pool, uint32_t oi) noexcept {
    BookOrder& o = pool[oi];
    Ladder& lad = side(o.side);
    PriceLevel& l = lad[o.level];
    if (o.prev != kNil) pool[o.prev].next = o.next; else l.head = o.next;
    if (o.next != kNil) pool[o.next].prev = o.prev; else l.tail = o.prev;
    l.qty -= o.qty;
    int& best = o.side == Side::Buy ? best_bid_ : best_ask_;
    const bool at_best = int(o.level) == best;
    if (--l.count == 0) {
      lad.unmark(o.level);
      if (at_best) best = o.side == Side::Buy ? lad.highest() : lad.lowest();
    }
    return at_best;
  }
  bool reduce(OrderPool& pool, uint32_t oi, uint32_t q) noexcept {  // q < o.qty
    BookOrder& o = pool[oi];
    side(o.side)[o.level].qty -= q;
    o.qty -= q;
    return int(o.level) == (o.side == Side::Buy ? best_bid_ : best_ask_);
  }
  // Overflow (outside window) is aggregated only; cold path, rare.
  void overflow_add(Side s, uint32_t px, int64_t q) {
    auto& v = s == Side::Buy ? ovf_bid_ : ovf_ask_;
    auto it = std::lower_bound(v.begin(), v.end(), px, [](auto& e, uint32_t p) { return e.first < p; });
    if (it != v.end() && it->first == px) { it->second += q; if (it->second == 0) v.erase(it); }
    else v.insert(it, {px, q});
  }
 private:
  Ladder bids_, asks_;
  int best_bid_ = -1, best_ask_ = -1;
  uint32_t base_ = 0, tick_ = 100;
  SymbolId sym_ = 0;
  std::vector<std::pair<uint32_t, int64_t>> ovf_bid_, ovf_ask_;
};

// Owns all books of a shard + the shared order index (ITCH refs are market-wide).
class BookManager {
 public:
  static constexpr uint16_t kNoBook = 0xFFFF;
  BookManager(uint32_t max_orders, int log2_map, uint16_t max_books)
      : pool_(max_orders), map_(log2_map), books_(max_books) { slot_.fill(kNoBook); }

  void add_symbol(SymbolId locate, uint32_t ref_px, uint32_t tick) {
    slot_[locate] = nbooks_;
    books_[nbooks_++].init(locate, ref_px, tick);
  }
  bool subscribed(SymbolId s) const noexcept { return slot_[s] != kNoBook; }
  OrderBook& book(SymbolId s) noexcept { return books_[slot_[s]]; }

  // Apply one event. Returns true when top-of-book of ev.sym changed.
  bool apply(const MarketEvent& ev) noexcept {
    switch (ev.type) {
      case MdType::Add: return add(ev.sym, ev.ref, ev.side, ev.px, ev.qty);
      case MdType::Execute:
      case MdType::ExecuteWithPrice:
      case MdType::Cancel: return reduce_or_remove(ev.ref, ev.qty);
      case MdType::Delete: return remove(ev.ref);
      case MdType::Replace: {
        const uint32_t oi = map_.find(ev.ref);
        if (oi == kNil) [[unlikely]] { ++unknown_ref_; return false; }
        const Side s = pool_[oi].side;
        const bool a = remove(ev.ref);
        const bool b = add(ev.sym, ev.aux, s, ev.px, ev.qty);
        return a || b;
      }
      default: return false;  // Trade / Broken / status handled elsewhere
    }
  }
  uint64_t unknown_refs() const noexcept { return unknown_ref_; }
  uint32_t live_orders() const noexcept { return pool_.in_use(); }

 private:
  bool add(SymbolId sym, uint64_t ref, Side s, uint32_t px, uint32_t qty) noexcept {
    OrderBook& b = book(sym);
    const uint32_t oi = pool_.alloc();
    if (oi == kNil) [[unlikely]] { ++pool_exhausted_; return false; }  // -> book invalid, alarm
    BookOrder& o = pool_[oi];
    o.ref = ref; o.px = px; o.qty = qty; o.sym = sym; o.side = s;
    map_.insert(ref, oi);
    uint32_t idx;
    if (!b.index_of(px, idx)) [[unlikely]] { o.level = kOverflowLevel; b.overflow_add(s, px, qty); return false; }
    o.level = uint16_t(idx);
    return b.link_back(pool_, oi);
  }
  bool reduce_or_remove(uint64_t ref, uint32_t q) noexcept {
    const uint32_t oi = map_.find(ref);
    if (oi == kNil) [[unlikely]] { ++unknown_ref_; return false; }
    BookOrder& o = pool_[oi];
    if (q >= o.qty) return remove_at(oi);
    if (o.level == kOverflowLevel) [[unlikely]] { book(o.sym).overflow_add(o.side, o.px, -int64_t(q)); o.qty -= q; return false; }
    return book(o.sym).reduce(pool_, oi, q);
  }
  bool remove(uint64_t ref) noexcept {
    const uint32_t oi = map_.find(ref);
    if (oi == kNil) [[unlikely]] { ++unknown_ref_; return false; }
    return remove_at(oi);
  }
  bool remove_at(uint32_t oi) noexcept {
    BookOrder& o = pool_[oi];
    bool changed = false;
    if (o.level == kOverflowLevel) [[unlikely]] book(o.sym).overflow_add(o.side, o.px, -int64_t(o.qty));
    else changed = book(o.sym).unlink(pool_, oi);
    map_.erase(o.ref);
    pool_.release(oi);
    return changed;
  }

  OrderPool pool_;
  OrderMap map_;
  std::vector<OrderBook> books_;
  std::array<uint16_t, 65536> slot_{};
  uint16_t nbooks_ = 0;
  uint64_t unknown_ref_ = 0, pool_exhausted_ = 0;
};

// ───────────────────────── Rate limiting ─────────────────────────
// Classic token bucket with lazy refill, integer "nano-tokens" (1 token = 1e9 units).
// refill: tokens += elapsed_ns * rate_per_sec   (ns * tokens/s == nano-tokens, no division)
class TokenBucket {
 public:
  static constexpr uint64_t kOne = 1'000'000'000ull;
  constexpr TokenBucket(uint64_t rate_per_sec, uint64_t burst) noexcept
      : rate_(rate_per_sec), cap_(burst * kOne), tokens_(burst * kOne),
        max_elapsed_(burst * kOne / rate_per_sec + 1) {}
  bool try_acquire(uint64_t now_ns, uint64_t n = 1) noexcept {
    refill(now_ns);
    const uint64_t cost = n * kOne;
    if (tokens_ < cost) return false;   // caller decides: drop / defer / downgrade; never sleep
    tokens_ -= cost;
    return true;
  }
  uint64_t available(uint64_t now_ns) noexcept { refill(now_ns); return tokens_ / kOne; }
 private:
  void refill(uint64_t now_ns) noexcept {
    if (now_ns <= last_ns_) return;                 // monotonic clock; ignore non-increasing stamps
    uint64_t el = now_ns - last_ns_;
    last_ns_ = now_ns;
    if (el > max_elapsed_) el = max_elapsed_;       // also prevents 64-bit overflow after long idle
    tokens_ = std::min(cap_, tokens_ + el * rate_);
  }
  uint64_t rate_, cap_, tokens_, max_elapsed_, last_ns_ = 0;
};

// GCRA (Generic Cell Rate Algorithm) == token bucket expressed in the time domain.
// One 64-bit state word, no division, no floating point, O(1).
class Gcra {
 public:
  constexpr Gcra(uint64_t rate_per_sec, uint64_t burst) noexcept
      : interval_ns_(1'000'000'000ull / rate_per_sec), tolerance_ns_((burst - 1) * interval_ns_) {}
  // Can we spend n tokens at time now? (pure, no side effect)
  bool can_acquire(uint64_t now, uint64_t n = 1) const noexcept {
    const uint64_t tat = tat_ns_ > now ? tat_ns_ : now;
    return tat + (n - 1) * interval_ns_ - now <= tolerance_ns_;
  }
  void commit(uint64_t now, uint64_t n = 1) noexcept {
    const uint64_t tat = tat_ns_ > now ? tat_ns_ : now;
    tat_ns_ = tat + n * interval_ns_;
  }
  bool try_acquire(uint64_t now, uint64_t n = 1) noexcept {
    if (!can_acquire(now, n)) return false;
    commit(now, n);
    return true;
  }
  // Remaining whole tokens (for headroom checks such as cancel reservation).
  uint64_t available(uint64_t now) const noexcept {
    const uint64_t tat = tat_ns_ > now ? tat_ns_ : now;
    const uint64_t used = tat - now;  // ns of "debt"
    return used > tolerance_ns_ + interval_ns_ ? 0 : (tolerance_ns_ + interval_ns_ - used) / interval_ns_;
  }
 private:
  uint64_t tat_ns_ = 0;  // theoretical arrival time
  uint64_t interval_ns_;
  uint64_t tolerance_ns_;
};

// Exact "at most N events in any window W" (e.g. a broker's msgs/sec rule).
// Ring of the last N accepted timestamps. O(1), N*8 bytes.
template <std::size_t N>
class SlidingWindowLimiter {
 public:
  explicit SlidingWindowLimiter(uint64_t window_ns) noexcept : window_ns_(window_ns) {}
  bool can_acquire(uint64_t now) const noexcept { return count_ < N || now - ts_[head_] >= window_ns_; }
  void commit(uint64_t now) noexcept {
    ts_[head_] = now;                 // overwrite the oldest
    head_ = (head_ + 1) % N;          // N is a compile-time constant
    if (count_ < N) ++count_;
  }
  bool try_acquire(uint64_t now) noexcept { if (!can_acquire(now)) return false; commit(now); return true; }
 private:
  std::array<uint64_t, N> ts_{};
  std::size_t head_ = 0, count_ = 0;
  uint64_t window_ns_;
};

}  // namespace hft

namespace hft {
// Raw on-the-wire layout of ITCH 5.0 'A' (documentation / static checks only).
// Multi-byte fields are big-endian byte arrays: never read them as native integers.
#pragma pack(push, 1)
struct ItchAddOrderWire {
  char    type;            // 'A'
  uint8_t stock_locate[2];
  uint8_t tracking[2];
  uint8_t timestamp[6];    // ns since midnight
  uint8_t order_ref[8];
  char    side;            // 'B' / 'S'
  uint8_t shares[4];
  char    stock[8];        // right-padded with spaces
  uint8_t price[4];        // Price(4): 4 implied decimals
};
#pragma pack(pop)
static_assert(sizeof(ItchAddOrderWire) == 36);
static_assert(offsetof(ItchAddOrderWire, order_ref) == 11 && offsetof(ItchAddOrderWire, price) == 32);

// Zero-copy view over one de-framed ITCH message inside the RX buffer.
struct ItchMessageView {
  const uint8_t* p;
  uint16_t len;
  char     type() const noexcept { return char(p[0]); }
  SymbolId locate() const noexcept { return be16(p + 1); }
  uint64_t timestamp_ns() const noexcept { return be48(p + 5); }
};
}  // namespace hft
