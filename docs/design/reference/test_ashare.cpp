// A-share component tests: L2 book reconstruction (SZSE + SSE semantics) against a matching-engine
// simulator, bitmap, T+1, fees/break-even, price cage, lots, budgets, cancel policy, pre-trade check.
#include "ashare.hpp"

#include <chrono>
#include <cstdio>
#include <deque>
#include <map>
#include <random>
#include <set>
#include <x86intrin.h>

using namespace hft;
using namespace hft::ashare;

#define CHECK(cond, msg) do { if (!(cond)) { std::printf("FAIL: %s (line %d)\n", msg, __LINE__); return 1; } } while (0)

static double tsc_ghz() {
  auto t0 = std::chrono::steady_clock::now(); uint64_t c0 = __rdtsc();
  while (std::chrono::steady_clock::now() - t0 < std::chrono::milliseconds(200)) {}
  auto t1 = std::chrono::steady_clock::now(); uint64_t c1 = __rdtsc();
  return double(c1 - c0) / std::chrono::duration<double, std::nano>(t1 - t0).count();
}
static void report(const char* name, std::vector<uint64_t>& cyc, double ghz) {
  std::sort(cyc.begin(), cyc.end());
  auto q = [&](double p) { return double(cyc[std::min(cyc.size() - 1, size_t(p * cyc.size()))]) / ghz; };
  std::printf("%-30s n=%-8zu P50=%7.1fns P90=%7.1fns P99=%7.1fns P99.9=%8.1fns Max=%9.1fns\n", name, cyc.size(),
              q(0.50), q(0.90), q(0.99), q(0.999), double(cyc.back()) / ghz);
}

// ───────────── minimal price-time matching engine that emits exchange-style L2 messages ─────────────
struct Sim {
  struct O { uint64_t seq; uint32_t qty; };
  std::map<uint32_t, std::deque<O>> bids, asks;     // price -> FIFO
  std::map<uint64_t, std::pair<Side, uint32_t>> where;  // seq -> (side, px)
  uint16_t channel, book; Semantics sem; uint64_t next = 1; uint32_t tick;
  std::vector<L2Event> out;

  L2Event base(L2Type t) { L2Event e{}; e.type = t; e.channel = channel; e.book = book; e.sem = sem; e.seq = next; return e; }
  uint32_t best(Side s) const {
    if (s == Side::Buy) return bids.empty() ? 0 : bids.rbegin()->first;
    return asks.empty() ? 0 : asks.begin()->first;
  }
  void rest(uint64_t seq, Side s, uint32_t px, uint32_t q) { (s == Side::Buy ? bids : asks)[px].push_back({seq, q}); where[seq] = {s, px}; }
  // match an incoming order; px_limit == 0 means market; max_levels limits price levels walked
  uint32_t match(uint64_t seq, Side s, uint32_t px_limit, uint32_t q, int max_levels, uint32_t& last_px) {
    int levels = 0;
    while (q && levels < max_levels) {
      auto& opp = s == Side::Buy ? asks : bids;
      if (opp.empty()) break;
      auto it = s == Side::Buy ? opp.begin() : std::prev(opp.end());
      if (px_limit && (s == Side::Buy ? it->first > px_limit : it->first < px_limit)) break;
      auto& dq = it->second;
      while (q && !dq.empty()) {
        O& r = dq.front();
        const uint32_t x = std::min(q, r.qty);
        L2Event t = base(L2Type::Trade); ++next;
        t.bid_key = make_key(channel, s == Side::Buy ? seq : r.seq);
        t.ask_key = make_key(channel, s == Side::Buy ? r.seq : seq);
        t.px = it->first; t.qty = x; t.side = s;
        out.push_back(t);
        q -= x; r.qty -= x; last_px = it->first;
        if (!r.qty) { where.erase(r.seq); dq.pop_front(); }
      }
      if (dq.empty()) opp.erase(it);
      ++levels;
    }
    return q;
  }
  void order(Side s, uint32_t px, uint32_t q, OrdKind kind, bool ioc) {
    const uint64_t seq = next;
    uint32_t eff_px = px, last = 0;
    if (kind == OrdKind::BestOwn) { eff_px = best(s); if (!eff_px) return; }
    if (kind == OrdKind::Market && !ioc) { eff_px = best(s == Side::Buy ? Side::Sell : Side::Buy); if (!eff_px) return; }  // 对手方最优
    if (sem == Semantics::Szse) {
      L2Event o = base(L2Type::Order); ++next;
      o.key = make_key(channel, seq); o.side = s; o.qty = q; o.kind = kind; o.px = kind == OrdKind::Limit ? px : 0;
      out.push_back(o);
    } else {
      ++next;  // SSE: order number allocated now, published only if a remainder rests
    }
    const int lv = (kind == OrdKind::Market && ioc) ? 5 : (kind == OrdKind::Market ? 1 : 1 << 20);
    const uint32_t lim = (kind == OrdKind::Market && ioc) ? 0 : eff_px;
    const uint32_t rem = match(seq, s, lim, q, lv, last);
    if (!rem) return;
    if (kind == OrdKind::Market && ioc) {
      if (sem == Semantics::Szse) { L2Event c = base(L2Type::Cancel); ++next; c.key = make_key(channel, seq); c.qty = rem; out.push_back(c); }
      return;
    }
    rest(seq, s, eff_px, rem);
    if (sem == Semantics::Sse) {
      L2Event o = base(L2Type::Order); ++next;
      o.key = make_key(channel, seq); o.side = s; o.qty = rem; o.px = eff_px; o.kind = OrdKind::Limit;
      out.push_back(o);
    }
  }
  bool cancel_random(std::mt19937_64& rng) {
    if (where.empty()) return false;
    auto it = where.begin(); std::advance(it, long(rng() % std::min<size_t>(where.size(), 64)));
    const uint64_t seq = it->first; const auto [s, px] = it->second;
    auto& lvl = (s == Side::Buy ? bids : asks)[px];
    for (auto d = lvl.begin(); d != lvl.end(); ++d) if (d->seq == seq) {
      L2Event c = base(L2Type::Cancel); ++next; c.key = make_key(channel, seq); c.qty = d->qty; out.push_back(c);
      lvl.erase(d); break;
    }
    if (lvl.empty()) (s == Side::Buy ? bids : asks).erase(px);
    where.erase(seq);
    return true;
  }
  int depth(Side s, int n, uint32_t* px, uint64_t* qty) const {
    int k = 0;
    auto put = [&](uint32_t p, const std::deque<O>& d) { uint64_t t = 0; for (auto& o : d) t += o.qty; px[k] = p; qty[k] = t; ++k; };
    if (s == Side::Buy) { for (auto it = bids.rbegin(); it != bids.rend() && k < n; ++it) put(it->first, it->second); }
    else { for (auto it = asks.begin(); it != asks.end() && k < n; ++it) put(it->first, it->second); }
    return k;
  }
};

int main() {
  const double ghz = tsc_ghz();

  // ───────── HierBitmap vs std::set ─────────
  {
    HierBitmap hb; const uint32_t n = 200'000; hb.init(n); std::set<uint32_t> ref; std::mt19937_64 rng(5);
    for (int i = 0; i < 400'000; ++i) {
      uint32_t x = uint32_t(rng() % n);
      if (rng() & 1) { hb.set(x); ref.insert(x); } else { hb.clear(x); ref.erase(x); }
      if (i % 50 == 0) {
        CHECK(hb.highest() == (ref.empty() ? -1 : int(*ref.rbegin())), "bitmap highest");
        CHECK(hb.lowest() == (ref.empty() ? -1 : int(*ref.begin())), "bitmap lowest");
        uint32_t qx = uint32_t(rng() % n);
        auto it = ref.lower_bound(qx); int pb = it == ref.begin() ? -1 : int(*std::prev(it));
        CHECK(hb.prev_below(qx) == pb, "bitmap prev_below");
        auto jt = ref.upper_bound(qx); int na = jt == ref.end() ? -1 : int(*jt);
        CHECK(hb.next_above(qx) == na, "bitmap next_above");
      }
    }
    std::puts("HierBitmap (200k levels) vs std::set: OK");
  }

  // ───────── L2 reconstruction vs simulator (SZSE + SSE semantics) ─────────
  SecurityRef sz{}; sz.sec_id = 0; sz.exch = Exchange::SZSE; sz.board = Board::Main; sz.type = InstrType::Stock;
  sz.tick = 100; sz.prev_close = 100'000; sz.limit_up = 110'000; sz.limit_down = 90'000;   // 10.00 CNY, ±10 %
  sz.buy_min = 100; sz.buy_step = 100; sz.sell_lot = 100; sz.cage_bps = 200; sz.cage_ticks = 10; sz.stamp_duty = true;
  SecurityRef sh = sz; sh.sec_id = 1; sh.exch = Exchange::SSE; sh.board = Board::Star; sh.prev_close = 500'000;
  sh.limit_up = 600'000; sh.limit_down = 400'000; sh.cage_ticks = 0; sh.buy_min = 200; sh.buy_step = 1;   // 50.00 CNY, ±20 %
  AshareBookBuilder bb(1u << 20, 21, 2);
  bb.add_security(sz); bb.add_security(sh); bb.add_channel(2011); bb.add_channel(6);
  Sim ssz{{}, {}, {}, 2011, 0, Semantics::Szse, 1, 100, {}};
  Sim ssh{{}, {}, {}, 6, 1, Semantics::Sse, 1, 100, {}};
  std::mt19937_64 rng(11);
  std::vector<L2Event> stream; stream.reserve(4'000'000);
  auto step = [&](Sim& s, uint32_t mid, bool szse) {
    const int r = int(rng() % 100);
    const Side side = (rng() & 1) ? Side::Buy : Side::Sell;
    const uint32_t q = 100 * (1 + uint32_t(rng() % 10));
    if (s.where.size() < 200 || r < 55) {
      int off = int(rng() % 12) - 3;  // some orders cross
      uint32_t px = side == Side::Buy ? mid - uint32_t(off) * 100 : mid + uint32_t(off) * 100;
      s.order(side, px, q, OrdKind::Limit, false);
    } else if (r < 62) s.order(side, 0, q, OrdKind::Market, true);                 // IOC market (最优五档即时成交剩余撤销)
    else if (r < 67) s.order(side, 0, q, OrdKind::Market, false);                  // 对手方最优 / 剩余转限价
    else if (r < 70 && szse) s.order(side, 0, q, OrdKind::BestOwn, false);         // 本方最优 (SZSE)
    else s.cancel_random(rng);
  };
  uint32_t px[10], spx[10]; uint64_t qy[10], sqy[10];
  for (int i = 0; i < 300'000; ++i) {
    for (Sim* s : {&ssz, &ssh}) {
      s->out.clear();
      step(*s, s == &ssz ? 100'000 : 500'000, s == &ssz);
      for (auto& e : s->out) { bb.apply(e); stream.push_back(e); }
      bb.flush_all();   // burst complete (timer in production)
      if (i % 7 == 0) {
        for (Side sd : {Side::Buy, Side::Sell}) {
          const int a = bb.book(s->book).depth(sd, 10, px, qy), b = s->depth(sd, 10, spx, sqy);
          bool ok = a == b;
          for (int k = 0; ok && k < a; ++k) ok = px[k] == spx[k] && qy[k] == sqy[k];
          if (!ok) { std::printf("L2 MISMATCH i=%d book=%u side=%d levels %d vs %d\n", i, s->book, int(sd), a, b); return 1; }
        }
      }
    }
  }
  const auto& st = bb.stats();
  std::printf("L2 book (SZSE+SSE semantics): %zu messages, 10-level depth verified vs matching simulator; "
              "unknown_ref=%llu sse_aggressor_absent=%llu crossed=%llu off_ladder=%llu live=%u\n",
              stream.size(), (unsigned long long)st.unknown_ref, (unsigned long long)st.sse_aggressor_absent,
              (unsigned long long)st.crossed_after_flush, (unsigned long long)st.off_ladder, bb.live_orders());
  CHECK(st.unknown_ref == 0 && st.crossed_after_flush == 0 && st.off_ladder == 0, "L2 builder anomalies");

  // latency of apply() on a fresh replay of the same stream
  {
    AshareBookBuilder b2(1u << 20, 21, 2);
    b2.add_security(sz); b2.add_security(sh); b2.add_channel(2011); b2.add_channel(6);
    std::vector<uint64_t> cyc; cyc.reserve(stream.size());
    for (auto& e : stream) {
      _mm_lfence(); uint64_t a = __rdtsc(); _mm_lfence();
      b2.apply(e);
      _mm_lfence(); uint64_t c = __rdtsc(); _mm_lfence();
      cyc.push_back(c - a);
    }
    report("AshareBookBuilder::apply", cyc, ghz);
  }

  // ───────── T+1 position ─────────
  {
    T1Position p; p.yd_qty = 1000;
    p.bought_today = 500; CHECK(p.sellable() == 1000, "T+1: today's buys not sellable");
    p.sold_today = 300; p.frozen_sell = 200; CHECK(p.sellable() == 500, "T+1 sellable after sells/frozen");
    CHECK(p.position() == 1200 && p.net_bought_today() == 200, "T+1 position");
    p.t0 = true; CHECK(p.sellable() == 1000, "T+0 instrument: buys sellable");
    std::puts("T+1 / T+0 sellable logic: OK");
  }

  // ───────── fees & break-even table ─────────
  {
    FeeSchedule f;  // 万1 commission, no minimum, stamp 0.05 %, transfer 0.001 %, exchange fees bundled
    SecurityRef st_ = sz; st_.limit_up = 0;
    std::puts("Break-even (stock, tick 0.01, notional ~20k CNY, 万1 commission, stamp 0.05 % sell, transfer 0.001 %):");
    std::puts("  price   tick(bps)  breakeven_ticks  round_trip_cost(bps)");
    for (double P : {3.0, 5.0, 8.0, 10.0, 13.0, 15.0, 20.0, 50.0, 100.0}) {
      const Price pxv = Price(P * 10'000); Qty q = Qty(20'000 / P) / 100 * 100; if (q < 100) q = 100;
      const int64_t k = breakeven_ticks(f, st_, pxv, q);
      const int64_t cost = fee_for_fill(f, st_, Side::Buy, pxv * q) + fee_for_fill(f, st_, Side::Sell, pxv * q);
      std::printf("  %6.2f  %8.2f  %15lld  %18.2f\n", P, 100.0 / P, (long long)k, double(cost) / double(pxv * q) * 1e4);
    }
    CHECK(breakeven_ticks(f, st_, 100'000, 2000) == 1, "10 CNY stock: 1 tick covers costs");
    CHECK(breakeven_ticks(f, st_, 200'000, 1000) == 2, "20 CNY stock: needs 2 ticks");
    FeeSchedule fmin = f; fmin.commission_min = 50'000;   // 5 CNY minimum commission
    CHECK(breakeven_ticks(fmin, st_, 100'000, 100) > breakeven_ticks(f, st_, 100'000, 100), "minimum commission hurts small orders");
    std::printf("  with a 5-CNY minimum commission, 100 sh @ 10.00 needs %lld ticks (vs %lld)\n",
                (long long)breakeven_ticks(fmin, st_, 100'000, 100), (long long)breakeven_ticks(f, st_, 100'000, 100));
    SecurityRef etf = sz; etf.type = InstrType::Etf; etf.stamp_duty = false; etf.tick = 10; etf.limit_up = 0;
    SecurityRef cb = etf; cb.type = InstrType::ConvBond;
    FeeSchedule fe = f; fe.commission_rate = 5'000;   // e.g. 万0.5 (negotiated)
    std::printf("  ETF  @1.000 (tick 0.001, no stamp duty, 万0.5): breakeven %lld ticks, tick = %.1f bps\n",
                (long long)breakeven_ticks(fe, etf, 10'000, 20'000), 10.0);
    std::printf("  CB   @120.000 (tick 0.001, no stamp duty, 万0.5): breakeven %lld ticks, tick = %.3f bps\n",
                (long long)breakeven_ticks(fe, cb, 1'200'000, 200), 0.001 / 120.0 * 1e4);
  }

  // ───────── price cage / limits / lots ─────────
  {
    CHECK(price_cage_ok(sz, Side::Buy, 102'000, 100'000) && !price_cage_ok(sz, Side::Buy, 102'100, 100'000), "cage 10.00 buy cap 10.20");
    SecurityRef low = sz; low.prev_close = 30'000; low.limit_up = 33'000; low.limit_down = 27'000;
    CHECK(price_cage_ok(low, Side::Buy, 31'000, 30'000) && !price_cage_ok(low, Side::Buy, 31'100, 30'000), "cage 3.00 buy cap 3.10 (10 ticks)");
    CHECK(price_cage_ok(low, Side::Sell, 29'000, 30'000) && !price_cage_ok(low, Side::Sell, 28'900, 30'000), "cage 3.00 sell floor 2.90");
    SecurityRef star = low; star.cage_ticks = 0;
    CHECK(price_cage_ok(star, Side::Buy, 30'600, 30'000) && !price_cage_ok(star, Side::Buy, 30'700, 30'000), "STAR cage 2 % only");
    CHECK(price_limit_ok(sz, 110'000) && !price_limit_ok(sz, 110'100) && !price_limit_ok(sz, 89'900), "price limits");
    CHECK(lot_ok(sz, Side::Buy, 300, 0) && !lot_ok(sz, Side::Buy, 150, 0), "main board buy lots");
    CHECK(lot_ok(sz, Side::Sell, 150, 150) && lot_ok(sz, Side::Sell, 50, 150) && !lot_ok(sz, Side::Sell, 120, 150), "odd-lot sell remainder");
    CHECK(lot_ok(sh, Side::Buy, 201, 0) && !lot_ok(sh, Side::Buy, 199, 0), "STAR buy: >=200, step 1");
    std::puts("Price cage / price limit / lot rules: OK");
  }

  // ───────── budgets & cancel policy ─────────
  {
    const uint64_t session = 14'220ull * 1'000'000'000ull;   // 9:30-11:30 + 13:00-14:57
    DailyMessageBudget b(18'000, 1'000, 500, session);
    CHECK(b.allowance(0) == 500 && b.allowance(session / 2) == 500 + 8'250 && b.allowance(session) == 17'000, "budget pacing");
    for (int i = 0; i < 500; ++i) { CHECK(b.can_spend(0, false), "burst"); b.spend(); }
    CHECK(!b.can_spend(0, false) && b.can_spend(0, true), "paced out but emergency allowed");
    CancelPolicy cp(1'000'000'000ull, 2, 60, 50);
    CHECK(cp.check(500'000'000, 0, false) == CancelPolicy::Decision::Allow, "instant cancel allowed within quota");
    cp.on_cancel(500'000'000, 0); cp.on_cancel(600'000'000, 0);
    CHECK(cp.check(700'000'000, 0, false) == CancelPolicy::Decision::Defer, "instant quota used -> defer");
    CHECK(cp.check(700'000'000, 0, true) == CancelPolicy::Decision::Allow, "emergency cancel always allowed");
    for (int i = 0; i < 100; ++i) cp.on_order();
    for (int i = 0; i < 58; ++i) cp.on_cancel(5'000'000'000ull, 0);
    CHECK(cp.check(9'000'000'000ull, 0, false) == CancelPolicy::Decision::Deny, "cancel ratio cap");
    std::puts("Daily message budget / cancel policy: OK");
  }

  // ───────── A-share pre-trade check ─────────
  {
    std::atomic<uint64_t> blk{0};
    DailyMessageBudget budget(18'000, 1'000, 500, 14'220ull * 1'000'000'000ull);
    SlidingWindowLimiter<100> ps(1'000'000'000ull);
    AAccountState a{&blk, {200'000'000'000ll /*20M CNY*/, 0}, FeeSchedule{}, 0, 300'000'000ll /*30k CNY*/,
                    SessionPhase::Continuous, 3'000'000'000u, 1'000'000'000ull, &budget, &ps};
    ASymbolState s{&sz, {}, 99'900, 100'000, 0, 0, 20'000, 5'000, 0, 0, false};
    s.pos.yd_qty = 10'000;
    const uint64_t now = 2'000'000'000ull; s.last_md_ts32 = uint32_t(now);
    AOrderReq buy{99'900, 1'000, Side::Buy, false, false, true};
    AOrderReq sell{100'000, 1'000, Side::Sell, false, false, true};
    CHECK(ashare_pre_trade_check(buy, s, a, now) == AVerdict::Ok, "base buy ok");
    CHECK(ashare_pre_trade_check(sell, s, a, now) == AVerdict::Ok, "base sell ok");
    AOrderReq x = buy; x.px = 100'000; CHECK(ashare_pre_trade_check(x, s, a, now) == AVerdict::WouldCross, "no post-only: cross check");
    x = buy; x.px = 99'950; CHECK(ashare_pre_trade_check(x, s, a, now) == AVerdict::OffTick, "tick grid");
    x = buy; x.px = 90'000; CHECK(ashare_pre_trade_check(x, s, a, now) == AVerdict::AtLimitPrice, "no quotes at limit-down");
    x = buy; x.qty = 150; CHECK(ashare_pre_trade_check(x, s, a, now) == AVerdict::BadLot, "lot");
    s.own_best_ask = 99'900; CHECK(ashare_pre_trade_check(buy, s, a, now) == AVerdict::SelfTrade, "self-trade prevention"); s.own_best_ask = 0;
    T1Position keep = s.pos; s.pos.sold_today = 9'500;
    CHECK(ashare_pre_trade_check(sell, s, a, now) == AVerdict::InsufficientSellable, "T+1 sellable"); s.pos = keep;
    s.pos.bought_today = 4'500; CHECK(ashare_pre_trade_check(buy, s, a, now) == AVerdict::OvernightCap, "overnight net-buy cap"); s.pos = keep;
    a.cash.available = 50'000'000; CHECK(ashare_pre_trade_check(buy, s, a, now) == AVerdict::InsufficientCash, "cash"); a.cash.available = 200'000'000'000ll;
    a.phase = SessionPhase::CloseAuction; CHECK(ashare_pre_trade_check(buy, s, a, now) == AVerdict::SessionClosed, "no orders in closing auction"); a.phase = SessionPhase::Continuous;
    blk = kBlkReduceOnly; CHECK(ashare_pre_trade_check(buy, s, a, now) == AVerdict::ReduceOnly, "reduce-only blocks buys");
    CHECK(ashare_pre_trade_check(sell, s, a, now) == AVerdict::Ok, "reduce-only allows sells");
    blk = kBlkKill; CHECK(ashare_pre_trade_check(sell, s, a, now) == AVerdict::KillSwitch, "kill blocks everything new");
    AOrderReq c{0, 0, Side::Buy, true, true, false}; CHECK(ashare_pre_trade_check(c, s, a, now) == AVerdict::Ok, "cancel under kill");
    blk = 0;
    std::puts("A-share pre-trade check: OK");

    std::vector<uint64_t> cyc; cyc.reserve(1'000'000); uint64_t okc = 0;
    for (int i = 0; i < 1'000'000; ++i) {
      const AOrderReq& r = (i & 1) ? buy : sell;
      _mm_lfence(); uint64_t t0 = __rdtsc(); _mm_lfence();
      okc += ashare_pre_trade_check(r, s, a, now) == AVerdict::Ok;
      _mm_lfence(); uint64_t t1 = __rdtsc(); _mm_lfence();
      cyc.push_back(t1 - t0);
    }
    report("ashare_pre_trade_check (pass)", cyc, ghz);
    CHECK(okc == 1'000'000, "all pass");
  }

  // ───────── T+0 around base ─────────
  {
    T1Position p; p.yd_qty = 10'000;
    T0Limits l = t0_quote_limits(p, 10'000, 2'000, 3'000, false);
    CHECK(l.max_bid_qty == 2'000 && l.max_ask_qty == 2'000, "flat at base: symmetric band");
    p.sold_today = 2'000; l = t0_quote_limits(p, 10'000, 2'000, 3'000, false);
    CHECK(l.max_ask_qty == 0 && l.max_bid_qty == 4'000 && l.bid_on && !l.ask_on, "sold the band: no more sells; may buy up to base + band");
    p.sold_today = 0; p.bought_today = 1'500; l = t0_quote_limits(p, 10'000, 2'000, 3'000, true);
    CHECK(!l.bid_on && l.max_ask_qty == 1'500, "late session above base: only sell back to base (from yesterday's shares)");
    std::puts("T+0-around-base quote limits: OK");
  }
  std::puts("ALL OK");
  return 0;
}
