// Correctness tests (vs naive std::map book) + micro-benchmarks with per-op TSC histograms.
#include "risk.hpp"
#include <chrono>
#include <cstdio>
#include <map>
#include <random>
#include <thread>
#include <x86intrin.h>

using namespace hft;

static double tsc_ghz() {
  auto t0 = std::chrono::steady_clock::now(); uint64_t c0 = __rdtsc();
  while (std::chrono::steady_clock::now() - t0 < std::chrono::milliseconds(200)) {}
  auto t1 = std::chrono::steady_clock::now(); uint64_t c1 = __rdtsc();
  return double(c1 - c0) / std::chrono::duration<double, std::nano>(t1 - t0).count();
}
static void report(const char* name, std::vector<uint64_t>& cyc, double ghz) {
  std::sort(cyc.begin(), cyc.end());
  auto q = [&](double p) { return double(cyc[std::min(cyc.size() - 1, size_t(p * cyc.size()))]) / ghz; };
  std::printf("%-28s n=%-9zu P50=%7.1fns P90=%7.1fns P99=%7.1fns P99.9=%8.1fns Max=%9.1fns\n", name, cyc.size(),
              q(0.50), q(0.90), q(0.99), q(0.999), double(cyc.back()) / ghz);
}

// Encode helpers for building raw ITCH messages (test only)
static void put16(uint8_t* p, uint16_t v) { v = __builtin_bswap16(v); std::memcpy(p, &v, 2); }
static void put32(uint8_t* p, uint32_t v) { v = __builtin_bswap32(v); std::memcpy(p, &v, 4); }
static void put64(uint8_t* p, uint64_t v) { v = __builtin_bswap64(v); std::memcpy(p, &v, 8); }
static void put48(uint8_t* p, uint64_t v) { put16(p, uint16_t(v >> 32)); put32(p + 2, uint32_t(v)); }

int main() {
  const double ghz = tsc_ghz();
  std::printf("TSC ~ %.3f GHz\n", ghz);

  // ---------- ITCH decode round trip ----------
  {
    uint8_t m[64] = {};
    m[0] = 'A'; put16(m + 1, 42); put48(m + 5, 34'200'000'000'123ull); put64(m + 11, 987654321ull);
    m[19] = 'S'; put32(m + 20, 300); std::memcpy(m + 24, "AAPL    ", 8); put32(m + 32, 1'893'400);
    MarketEvent ev{};
    bool ok = decode_itch(m, 36, 7, 0, ev);
    if (!ok || ev.type != MdType::Add || ev.sym != 42 || ev.ref != 987654321ull || ev.side != Side::Sell ||
        ev.qty != 300 || ev.px != 1'893'400 || ev.exch_ts_ns != 34'200'000'000'123ull) { std::puts("DECODE FAIL"); return 1; }
    uint8_t u[64] = {}; u[0] = 'U'; put16(u + 1, 42); put64(u + 11, 5); put64(u + 19, 6); put32(u + 27, 100); put32(u + 31, 1'893'500);
    ok = decode_itch(u, 35, 8, 0, ev);
    if (!ok || ev.type != MdType::Replace || ev.ref != 5 || ev.aux != 6 || ev.qty != 100 || ev.px != 1'893'500) { std::puts("DECODE U FAIL"); return 1; }
    // decode throughput
    std::vector<uint8_t> buf; std::vector<uint16_t> lens; std::mt19937_64 rng(1);
    for (int i = 0; i < 1'000'000; ++i) {
      uint8_t x[64] = {}; int r = rng() % 4;
      if (r < 2) { x[0] = 'A'; put64(x + 11, i + 1); x[19] = 'B'; put32(x + 20, 100); put32(x + 32, 1'000'000); lens.push_back(36); }
      else if (r == 2) { x[0] = 'D'; put64(x + 11, i); lens.push_back(19); }
      else { x[0] = 'E'; put64(x + 11, i); put32(x + 19, 100); lens.push_back(31); }
      put16(x + 1, uint16_t(rng() % 8000)); buf.insert(buf.end(), x, x + lens.back());
    }
    uint64_t sum = 0; uint64_t c0 = __rdtsc(); size_t off = 0;
    for (size_t i = 0; i < lens.size(); ++i) { decode_itch(buf.data() + off, lens[i], i, 0, ev); off += lens[i]; sum += ev.ref + ev.sym; }
    uint64_t c1 = __rdtsc();
    std::printf("ITCH decode throughput: %.1f M msg/s (%.1f ns/msg) [chk %llu]\n",
                lens.size() / (double(c1 - c0) / ghz) * 1e3, double(c1 - c0) / ghz / lens.size(), (unsigned long long)(sum & 1));
  }

  // ---------- Order book correctness vs naive map ----------
  constexpr int kSyms = 64;
  BookManager bm(1u << 21, 22, kSyms);
  std::vector<std::map<uint32_t, uint64_t>> nb(kSyms), na(kSyms);
  for (int s = 0; s < kSyms; ++s) bm.add_symbol(SymbolId(s * 10 + 1), 500'000 + s * 10'000, 100);  // ~$50..$113
  struct Live { uint64_t ref; SymbolId sym; Side side; uint32_t px; uint32_t qty; };
  std::vector<Live> live; live.reserve(1 << 20);
  std::mt19937_64 rng(7);
  uint64_t next_ref = 1;
  std::vector<MarketEvent> evs; evs.reserve(6'000'000);
  auto gen = [&](bool track_naive) {
    MarketEvent e{};
    int r = int(rng() % 100);
    if (live.size() < 20'000 || r < 40) {  // add near touch
      int s = int(rng() % kSyms); SymbolId sym = SymbolId(s * 10 + 1);
      uint32_t mid = 500'000 + s * 10'000; Side side = (rng() & 1) ? Side::Buy : Side::Sell;
      int off = 1 + int(rng() % 20);  if (rng() % 1000 == 0) off += 3000;  // occasional far order -> overflow path
      uint32_t px = side == Side::Buy ? mid - off * 100 : mid + off * 100;
      uint32_t q = 100 * (1 + uint32_t(rng() % 5));
      e.type = MdType::Add; e.sym = sym; e.ref = next_ref++; e.side = side; e.px = px; e.qty = q;
      live.push_back({e.ref, sym, side, px, q});
      if (track_naive) (side == Side::Buy ? nb[s] : na[s])[px] += q;
    } else {
      size_t k = rng() % live.size(); Live& L = live[k]; int s = (L.sym - 1) / 10;
      auto& m = (L.side == Side::Buy ? nb[s] : na[s]);
      e.sym = L.sym; e.ref = L.ref;
      if (r < 55) {  // partial or full execute / cancel
        e.type = (r & 1) ? MdType::Execute : MdType::Cancel;
        uint32_t q = (rng() & 1) ? L.qty : 100 * (1 + uint32_t(rng() % 5)); if (q > L.qty) q = L.qty; e.qty = q;
        if (track_naive) { m[L.px] -= q; if (!m[L.px]) m.erase(L.px); }
        L.qty -= q; if (!L.qty) { L = live.back(); live.pop_back(); }
      } else if (r < 85) {
        e.type = MdType::Delete;
        if (track_naive) { m[L.px] -= L.qty; if (!m[L.px]) m.erase(L.px); }
        L = live.back(); live.pop_back();
      } else {  // replace: new ref, new px/qty, same side
        e.type = MdType::Replace; e.aux = next_ref++;
        uint32_t mid = 500'000 + s * 10'000; int off = 1 + int(rng() % 20);
        e.px = L.side == Side::Buy ? mid - off * 100 : mid + off * 100; e.qty = 100 * (1 + uint32_t(rng() % 5));
        if (track_naive) { m[L.px] -= L.qty; if (!m[L.px]) m.erase(L.px); m[e.px] += e.qty; }
        L.ref = e.aux; L.px = e.px; L.qty = e.qty;
      }
    }
    return e;
  };
  for (int i = 0; i < 2'000'000; ++i) {
    MarketEvent e = gen(true); bm.apply(e);
    if (i % 997 == 0) {
      for (int s = 0; s < kSyms; ++s) {
        TopOfBook t = bm.book(SymbolId(s * 10 + 1)).top();
        // naive best, ignoring overflow prices (outside ladder window by construction: off >= 3001 ticks)
        uint32_t nbp = 0; uint64_t nbq = 0, naq = 0; uint32_t nap = 0; uint32_t mid = 500'000 + s * 10'000;
        for (auto it = nb[s].rbegin(); it != nb[s].rend(); ++it) if (mid - it->first < 2048 * 100) { nbp = it->first; nbq = it->second; break; }
        for (auto it = na[s].begin(); it != na[s].end(); ++it) if (it->first - mid < 2048 * 100) { nap = it->first; naq = it->second; break; }
        if (t.bid_px != nbp || t.bid_qty != nbq || t.ask_px != nap || t.ask_qty != naq) {
          std::printf("BOOK MISMATCH i=%d sym=%d bid %u/%llu vs %u/%llu ask %u/%llu vs %u/%llu\n", i, s, t.bid_px,
                      (unsigned long long)t.bid_qty, nbp, (unsigned long long)nbq, t.ask_px, (unsigned long long)t.ask_qty, nap, (unsigned long long)naq);
          return 1;
        }
      }
    }
  }
  std::printf("Order book: 2,000,000 events verified vs std::map reference, live orders=%u unknown_refs=%llu\n",
              bm.live_orders(), (unsigned long long)bm.unknown_refs());

  // ---------- Order book latency (per event) ----------
  {
    for (int i = 0; i < 3'000'000; ++i) evs.push_back(gen(false));
    std::vector<uint64_t> cyc; cyc.reserve(evs.size());
    uint64_t chg = 0;
    for (auto& e : evs) {
      _mm_lfence(); uint64_t a = __rdtsc(); _mm_lfence();
      chg += bm.apply(e);
      _mm_lfence(); uint64_t b = __rdtsc(); _mm_lfence();
      cyc.push_back(b - a);
    }
    // rdtsc overhead
    std::vector<uint64_t> ov; ov.reserve(1'000'000);
    for (int i = 0; i < 1'000'000; ++i) { _mm_lfence(); uint64_t a = __rdtsc(); _mm_lfence(); _mm_lfence(); uint64_t b = __rdtsc(); _mm_lfence(); ov.push_back(b - a); }
    report("rdtsc+lfence overhead", ov, ghz);
    report("book.apply (64 syms)", cyc, ghz);
    std::printf("  top-of-book changes: %.1f%%\n", 100.0 * chg / evs.size());
    uint64_t c0 = __rdtsc(); for (auto& e : evs) chg += bm.apply(e); uint64_t c1 = __rdtsc();  // note: replays stale refs too
    (void)c0; (void)c1;
  }

  // ---------- GCRA / sliding window ----------
  {
    Gcra g(10, 5); int ok = 0;
    for (int i = 0; i < 100; ++i) ok += g.try_acquire(0);  // burst at t=0
    if (ok != 5) { std::printf("GCRA burst FAIL %d\n", ok); return 1; }
    ok = 0; for (uint64_t t = 0; t < 10'000'000'000ull; t += 1'000'000) ok += g.try_acquire(t);  // 10 s at 1 kHz attempts
    if (ok != 99) { std::printf("GCRA rate FAIL %d\n", ok); return 1; }  // burst already spent at t=0 -> one per 100ms from t=100ms
    SlidingWindowLimiter<50> sw(1'000'000'000ull); ok = 0;
    for (uint64_t t = 0; t < 3'000'000'000ull; t += 1'000'000) ok += sw.try_acquire(t);
    if (ok != 150) { std::printf("SW FAIL %d\n", ok); return 1; }
    TokenBucket tb(10, 5); ok = 0;
    for (int i = 0; i < 100; ++i) ok += tb.try_acquire(1);
    if (ok != 5) { std::printf("TB burst FAIL %d\n", ok); return 1; }
    ok = 0; for (uint64_t t = 1; t < 10'000'000'001ull; t += 1'000'000) ok += tb.try_acquire(t);
    if (ok != 99) { std::printf("TB rate FAIL %d\n", ok); return 1; }
    std::puts("GCRA / token bucket / sliding window: OK");
  }

  // ---------- pre-trade risk + kill switch ----------
  {
    std::atomic<uint64_t> blk{0};
    std::vector<SymbolRisk> sr(kSyms);
    for (auto& s : sr) { s = {}; s.max_position = 2000; s.ref_px = 500'000; s.best_bid = 499'900; s.best_ask = 500'100; }
    RiskState rs{};
    rs.global_block = &blk; rs.sym = sr.data();
    rs.lim = {1000, 4, 100, 500, 50'000'000'000ll /*$5M*/, 20'000'000'000ll, 50'000'000ll /*$5k*/, 500'000'000u};
    rs.order_rate = Gcra(1'000'000, 1000); rs.replace_rate = Gcra(1'000'000, 1000); rs.msg_rate = Gcra(1'000'000, 1000);
    OrderRequest r{}; r.qty = 100; r.px = 499'900; r.side = Side::Buy; r.action = OrderAction::New; r.post_only = true;
    uint64_t now = 1'000'000'000ull; for (auto& s : sr) s.last_md_ts32 = uint32_t(now);
    r.cl_ord_id = 1;
    if (pre_trade_check(r, rs, now) != RiskVerdict::Ok) { std::puts("RISK base FAIL"); return 1; }
    OrderRequest x = r; x.px = 500'100; if (pre_trade_check(x, rs, now) != RiskVerdict::WouldCross) { std::puts("RISK cross FAIL"); return 1; }
    x = r; x.px = 560'000; x.post_only = false; if (pre_trade_check(x, rs, now) != RiskVerdict::PriceCollar) { std::puts("RISK collar FAIL"); return 1; }
    x = r; x.qty = 5000; if (pre_trade_check(x, rs, now) != RiskVerdict::MaxOrderSize) { std::puts("RISK size FAIL"); return 1; }
    sr[0].open_buy = 1950; if (pre_trade_check(r, rs, now) != RiskVerdict::MaxPosition) { std::puts("RISK pos FAIL"); return 1; } sr[0].open_buy = 0;
    if (pre_trade_check(r, rs, now + 600'000'000ull) != RiskVerdict::StaleMarketData) { std::puts("RISK stale FAIL"); return 1; }
    KillSwitch ks({50'000'000ll}, &blk);
    ks.on_pnl(-31'000'000ll); if (ks.state() != KillState::Warning) { std::puts("KS warn FAIL"); return 1; }
    ks.on_pnl(-43'000'000ll); if (ks.state() != KillState::ReduceOnly) { std::puts("KS reduce FAIL"); return 1; }
    sr[0].position = 0; if (pre_trade_check(r, rs, now) != RiskVerdict::InventoryZone) { std::puts("RISK reduce-only FAIL"); return 1; }
    sr[0].position = 500; OrderRequest sell = r; sell.side = Side::Sell; sell.px = 500'100;
    if (pre_trade_check(sell, rs, now) != RiskVerdict::Ok) { std::puts("RISK reduce allowed FAIL"); return 1; }
    sr[0].position = 0;
    ks.on_pnl(-10'000'000ll); if (ks.state() != KillState::ReduceOnly) { std::puts("KS no-deescalate FAIL"); return 1; }
    bool tripped = ks.on_pnl(-50'000'000ll); if (!tripped || !(blk.load() & kBlkKill)) { std::puts("KS trip FAIL"); return 1; }
    if (pre_trade_check(r, rs, now) != RiskVerdict::KillSwitch) { std::puts("RISK kill FAIL"); return 1; }
    OrderRequest c = r; c.action = OrderAction::Cancel; if (pre_trade_check(c, rs, now) != RiskVerdict::Ok) { std::puts("RISK cancel-under-kill FAIL"); return 1; }
    if (ks.supervisor_reset({7, 1}, true, true)) { std::puts("KS reset-before-locked FAIL"); return 1; }
    ks.on_actions_complete();
    if (!ks.supervisor_reset({7, 1}, true, true) || (blk.load() & kBlkKill)) { std::puts("KS reset FAIL"); return 1; }
    { std::atomic<uint64_t> b2{0}; KillSwitch k2({50'000'000ll}, &b2);
      if (!k2.force_trip() || k2.force_trip() || !(b2.load() & kBlkKill)) { std::puts("KS force_trip FAIL"); return 1; } }
    std::puts("Risk checks + kill switch state machine: OK");

    // latency of the happy path
    std::vector<uint64_t> cyc; cyc.reserve(2'000'000); uint64_t okc = 0;
    std::mt19937_64 rr(3);
    for (int i = 0; i < 2'000'000; ++i) {
      r.sym = SymbolId(rr() % kSyms); r.cl_ord_id = 10 + i; r.side = (i & 1) ? Side::Buy : Side::Sell; r.px = (i & 1) ? 499'900 : 500'100;
      _mm_lfence(); uint64_t a = __rdtsc(); _mm_lfence();
      okc += pre_trade_check(r, rs, now) == RiskVerdict::Ok;
      _mm_lfence(); uint64_t b = __rdtsc(); _mm_lfence();
      cyc.push_back(b - a);
    }
    report("pre_trade_check (pass)", cyc, ghz);
    std::printf("  passed: %llu\n", (unsigned long long)okc);
  }

  // ---------- quote computation ----------
  {
    QuoteParams p{0.01, 1.0, 1.0, 1.0, 1.0, 1.0, 0.5, 0.3, 0.5, 0.0, 1.0, 2.0, 0.003, 500, 100, 5000};
    Features f{50.005, 50.006, 0.2, 0.1, 0.0, 0.004, 2000};
    QuotePair q0 = compute_quotes(f, 50.00, 50.01, 0, p);
    QuotePair ql = compute_quotes(f, 50.00, 50.01, 4000, p);
    QuotePair qs = compute_quotes(f, 50.00, 50.01, -4000, p);
    std::printf("quotes flat : bid %.4f x %lld  ask %.4f x %lld\n", q0.bid_px / 1e4, (long long)q0.bid_qty, q0.ask_px / 1e4, (long long)q0.ask_qty);
    std::printf("quotes long : bid %.4f x %lld  ask %.4f x %lld\n", ql.bid_px / 1e4, (long long)ql.bid_qty, ql.ask_px / 1e4, (long long)ql.ask_qty);
    std::printf("quotes short: bid %.4f x %lld  ask %.4f x %lld\n", qs.bid_px / 1e4, (long long)qs.bid_qty, qs.ask_px / 1e4, (long long)qs.ask_qty);
    if (ql.bid_qty != 100 || qs.ask_qty != 100) { std::puts("QUOTE size FAIL"); return 1; }
    if (!(q0.bid_px < q0.ask_px && ql.bid_px <= q0.bid_px && ql.ask_px <= q0.ask_px && ql.bid_qty < q0.bid_qty &&
          qs.ask_qty < q0.ask_qty && qs.bid_px >= q0.bid_px)) { std::puts("QUOTE skew FAIL"); return 1; }
    std::vector<uint64_t> cyc; cyc.reserve(1'000'000); int64_t sink = 0;
    for (int i = 0; i < 1'000'000; ++i) {
      f.imbalance = (i % 200 - 100) / 100.0;
      _mm_lfence(); uint64_t a = __rdtsc(); _mm_lfence();
      QuotePair q = compute_quotes(f, 50.00, 50.01, (i % 9000) - 4500, p);
      _mm_lfence(); uint64_t b = __rdtsc(); _mm_lfence();
      sink += q.bid_px; cyc.push_back(b - a);
    }
    report("compute_quotes", cyc, ghz);
    if (sink == 42) std::puts("");
  }

  // ---------- SPSC cross-core ping-pong ----------
  {
    static SpscRing<MarketEvent, 1024> a2b, b2a;
    constexpr int N = 200'000;
    std::thread t([] { MarketEvent e; for (int i = 0; i < N; ++i) { while (!a2b.try_pop(e)) {} while (!b2a.try_push(e)) {} } });
    std::vector<uint64_t> rt; rt.reserve(N); MarketEvent e{}; MarketEvent back;
    for (int i = 0; i < N; ++i) {
      e.seq = i; uint64_t s = __rdtsc();
      while (!a2b.try_push(e)) {}
      while (!b2a.try_pop(back)) {}
      rt.push_back(__rdtsc() - s);
      if (back.seq != uint64_t(i)) { std::puts("SPSC order FAIL"); return 1; }
    }
    t.join();
    for (auto& x : rt) x /= 2;  // one-way estimate
    report("SPSC one-way (RTT/2)", rt, ghz);
  }
  std::puts("ALL OK");
  return 0;
}
