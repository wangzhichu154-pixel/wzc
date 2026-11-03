#pragma once
#include "trading/itch.hpp"
#include <cstdint>
#include <map>
#include <unordered_map>

namespace trading {
class TokenBucket {
    static constexpr std::uint64_t unit=1'000'000'000ULL;
    std::uint64_t capacity_,credit_,rate_,last_;
public:
    TokenBucket(std::uint64_t burst,std::uint64_t rate,std::uint64_t now);
    bool refill(std::uint64_t now) noexcept;
    bool available(std::uint64_t cost=1) const noexcept;
    bool take(std::uint64_t cost=1) noexcept;
};
struct RiskLimits {
    Qty max_order{100};
    std::int64_t max_position{1'000};
    std::uint64_t max_notional{100'000'000'000ULL}; // price units * shares
    std::size_t max_open_orders{100};
    std::int64_t daily_loss{10'000'000};
    std::int64_t daily_drawdown{10'000'000};
    std::uint64_t max_market_age_ns{1'000'000'000};
    std::uint64_t orders_per_second{20};
    Price price_low{1},price_high{1'000'000'000};
};
struct RiskContext {
    bool market_valid{},broker_healthy{},system_healthy{},trading{};
    std::uint64_t now_ns{},market_ns{};
};
struct OrderRequest { RefId id{}; Side side{}; Qty quantity{}; Price price{}; };
enum class RiskReject {
    None,Disabled,Unhealthy,StaleMarket,BadRequest,Duplicate,
    Inventory,Notional,Capacity,Throttled,Loss
};
enum class LocalOrderState { PendingNew,Live,Partial,PendingCancel,Filled,Canceled,Rejected,Unknown };
struct Reservation {
    OrderRequest request;
    std::uint64_t leaves;
    LocalOrderState state;
    bool reserved;
};
struct PnL {
    std::int64_t realized{},unrealized{},fees{},rebates{},net{},high_watermark{};
    bool mark_valid{false};
};
// One instrument/account risk partition. No concurrent writers. Cross-instrument
// deployment requires a separate synchronous aggregate/credit allocator.
class RiskEngine {
    RiskLimits limits_;
    TokenBucket orders_rate_;
    std::map<RefId,Reservation> orders_;
    struct Fill { RefId order; Qty quantity; Price price; };
    std::unordered_map<std::uint64_t,Fill> executions_;
    std::int64_t position_{0};
    std::uint64_t buys_{0},sells_{0},epoch_{1};
    std::size_t open_count_{0};
    bool killed_{false},armed_{false};
    PnL pnl_;
    void release(Reservation&) noexcept;
public:
    explicit RiskEngine(RiskLimits limits={},std::uint64_t now=0);
    bool arm_after_reconciliation() noexcept;
    RiskReject check_and_reserve(const OrderRequest&,const RiskContext&);
    bool acknowledge(RefId);
    bool request_cancel(RefId); // Does NOT release exposure.
    bool confirm_terminal(RefId,LocalOrderState);
    bool mark_unknown(RefId);
    bool fill(std::uint64_t execution_id,RefId,Qty,Price);
    void update_pnl(std::int64_t realized,std::int64_t unrealized,std::int64_t fees,std::int64_t rebates,bool mark_valid);
    void kill() noexcept { if(!killed_){killed_=true;armed_=false;++epoch_;} }
    bool killed() const noexcept { return killed_; }
    bool armed() const noexcept { return armed_; }
    std::size_t open_order_count() const noexcept { return open_count_; }
    std::uint64_t epoch() const noexcept { return epoch_; }
    std::int64_t position() const noexcept { return position_; }
    std::int64_t min_position() const noexcept { return position_-static_cast<std::int64_t>(sells_); }
    std::int64_t max_position() const noexcept { return position_+static_cast<std::int64_t>(buys_); }
    const PnL& pnl() const noexcept { return pnl_; }
    const auto& orders() const noexcept { return orders_; }
};
}
