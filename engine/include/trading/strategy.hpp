#pragma once
#include "trading/book.hpp"
#include <cstdint>
#include <optional>
namespace trading {
inline constexpr std::int64_t max_quote_economic_component_units=1'000'000'000;
inline constexpr std::int64_t max_quote_cost_floor_units=4'000'000'000;
inline constexpr char quote_economics_policy_version[]="conservative_one_share_v2";
struct PricingConfig {
    Price tick{100}; // Explicit configured tick, not implied by ITCH wire scale.
    Price min_half_spread{100};
    Price max_inventory_skew{500};
    std::int64_t hard_inventory{1'000};
    Qty quote_size{100};
    std::int64_t fee_per_share_units{0};
    std::int64_t min_fee_per_order_units{0};
    std::int64_t slippage_buffer_units{0};
    std::int64_t min_net_edge_units{0};
};
struct Quote { Price bid{},ask{};Qty bid_size{},ask_size{}; };
// Deliberately conservative: allocate the entire order minimum to one share,
// then add per-share fee. This bounds max(minimum, variable fee) from above for
// the smallest possible fill; rebates are not assumed when pricing.
Price quote_cost_floor(const PricingConfig&);
std::optional<Quote> price_quote(const Top&,std::int64_t inventory,const PricingConfig&);
}
