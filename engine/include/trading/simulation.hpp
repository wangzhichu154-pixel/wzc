#pragma once
#include "trading/book.hpp"
#include "trading/risk.hpp"
#include "trading/strategy.hpp"
#include <array>
#include <deque>
#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>

namespace trading {
struct SimulationConfig {
    Qty max_order_size{100};
    std::int64_t soft_inventory_limit{500},hard_inventory_limit{1000};
    std::int64_t daily_loss_limit_units{10'000'000};
    std::uint64_t orders_per_second{5};
    std::uint64_t latency_ns{1'000'000},cancel_latency_ns{1'000'000};
    std::uint64_t quote_lifetime_ns{1'000'000'000};
    std::int64_t fee_per_share_units{30},rebate_per_share_units{0};
    std::int64_t min_fee_per_order_units{0},slippage_buffer_units{0},min_net_edge_units{0};
    Price tick_size{100};
    bool participation_gate_required{false};
    bool long_only{false};
    std::uint64_t trade_start_ns{0},trade_end_ns{86'400'000'000'000ULL};
    std::uint64_t max_signal_age_ns{1'000'000'000};
};
struct SimulationStats {
    std::uint64_t decisions{},orders_submitted{},orders_activated{},orders_canceled{};
    std::uint64_t risk_rejections{},fill_count{},filled_quantity{};
    std::uint64_t queue_consumed{},cancel_requests{},fills_while_cancel_pending{};
    std::uint64_t post_only_rejections{};
    std::uint64_t cost_widened_decisions{},no_valid_quote_decisions{};
    std::uint64_t buy_quantity{},sell_quantity{},buy_notional{},sell_notional{},turnover{};
    std::uint64_t peak_abs_inventory{},peak_abs_inventory_in_window{};
    std::uint64_t closed_inventory_episodes{},holding_total_ns{},holding_max_ns{};
    std::uint64_t window_observed_ns{},signals_applied{},expired_signal_rows{},gate_denied_decisions{};
    std::uint64_t fills_after_window_end{},quantity_after_window_end{},turnover_after_window_end{};
    std::uint64_t long_only_rejections{};
    std::int64_t realized{},unrealized{},fees{},rebates{},net{},max_drawdown{},cash{};
    bool pnl_mark_valid{true};
};
// Explicit single-instrument counterfactual simulator. It never calls a broker.
// Historical displayed executions are eligibility evidence for an internal
// synthetic execution; they are never directly imported as account executions.
class Simulator {
    struct WorkingOrder {
        RefId id{};Side side{};Price price{};Qty leaves{};
        std::uint64_t submitted_ns{},active_after_ns{},activation_sequence{},cancel_after_ns{};
        bool active{},cancel_pending{};
        std::unordered_map<RefId,std::uint64_t> ahead;
        std::uint64_t ahead_quantity{};
    };
    struct Lot { std::int64_t quantity;Price price; };
    struct OrderFeeState { std::uint64_t filled_quantity{};std::int64_t charged_units{}; };
    SimulationConfig config_;
    RiskEngine risk_;
    PricingConfig pricing_;
    std::array<std::optional<WorkingOrder>,2> working_;
    std::deque<Lot> lots_;
    std::unordered_set<std::uint64_t> fill_evidence_matches_;
    // Retained by order ID through terminal state; a canceled/replaced working
    // slot must not erase prior commission allocation for that order.
    std::unordered_map<RefId,OrderFeeState> order_fees_;
    SimulationStats stats_;
    std::optional<std::uint16_t> symbol_;
    std::uint64_t sequence_{0},order_id_{0},execution_id_{0},now_{0};
    std::uint64_t decision_hash_{14695981039346656037ULL};
    std::optional<std::uint64_t> last_valid_bbo_ns_;
    Top previous_top_;
    bool gate_seen_{false},gate_bid_{false},gate_ask_{false},window_end_observed_{false};
    std::uint64_t gate_expires_ns_{0},last_signal_ordinal_{0};
    std::optional<std::uint64_t> first_event_ns_,holding_start_ns_;
    std::int64_t inventory_at_window_end_{0};
    long double inventory_share_ns_{0};
    // Reporting-only account valuation. This never arms risk or authorizes a quote.
    bool valuation_path_complete_{true};
    std::int64_t valuation_high_watermark_{0},valuation_max_drawdown_{0};
    void cancel(std::size_t);
    void cancel_at(std::size_t,std::uint64_t);
    void account_fill(RefId,Side,Qty,Price);
    void mark(const Event&,const Book&);
    void observe_account_valuation();
    void submit(Side,Price,Qty);
    bool side_enabled(Side) const noexcept;
public:
    explicit Simulator(SimulationConfig);
    // Call before and after Book::apply for each event; same ordered stream.
    void before_event(const Event&,const Book&,std::uint64_t monotonic_ns,std::uint64_t sequence);
    void after_event(const Event&,const Book&);
    // Called only after this exact event's book update and before after_event.
    // It can suppress sides; pricing, size, risk and order lifecycle remain
    // authoritative. Expired signals explicitly clear permissions.
    void set_participation(std::uint64_t ordinal,bool allow_bid,bool allow_ask,std::uint64_t expires_ns);
    const SimulationStats& stats() const noexcept{return stats_;}
    const RiskEngine& risk() const noexcept{return risk_;}
    std::uint64_t decision_checksum() const noexcept{return decision_hash_;}
    std::size_t open_orders() const noexcept;
    std::string json() const;
};
}
