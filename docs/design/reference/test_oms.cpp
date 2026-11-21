#include "oms.hpp"
#include <cstdio>
using namespace hft;
template <ExecutionGateway G> int drive(G& g) { OrderRequest r{}; ExecSinkProbe s; g.send_new(r); g.send_cancel(r); return g.poll(s); }
int main() {
  SimGateway g; drive(g);
  HugeArena a(64ull << 20);
  auto* snap = a.make<MarketSnapshot>();
  auto* st = a.make<StrategyState>();
  std::printf("arena ok=%d huge=%d snap=%zu strat=%zu order=%zu exec=%zu pnl=%zu sent=%llu aligned=%d\n", a.ok(), a.huge(),
    sizeof(MarketSnapshot), sizeof(StrategyState), sizeof(Order), sizeof(ExecutionReport), sizeof(PnLState),
    (unsigned long long)g.sent_, int((reinterpret_cast<uintptr_t>(snap) % 64) == 0 && (reinterpret_cast<uintptr_t>(st) % 64) == 0));
}
