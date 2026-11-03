#include "trading/strategy.hpp"
#include <algorithm>
#include <cmath>
namespace trading {
Price quote_cost_floor(const PricingConfig& c) {
    Price total=0;
    for(const auto value:{c.fee_per_share_units,c.min_fee_per_order_units,c.slippage_buffer_units,c.min_net_edge_units}) {
        if(value<0||value>max_quote_economic_component_units||value>max_quote_cost_floor_units-total)
            throw ProtocolError("quote economics outside supported nonnegative bounds");
        total+=value;
    }
    return total;
}
std::optional<Quote> price_quote(const Top& t,std::int64_t q,const PricingConfig& c) {
    if(c.tick<=0||c.min_half_spread<=0||c.hard_inventory<=0||c.hard_inventory>1'000'000'000||c.max_inventory_skew<0||!c.quote_size)
        throw ProtocolError("invalid pricing configuration");
    const auto cost_floor=quote_cost_floor(c);
    if(!t.valid||t.bid<=0||t.ask<=t.bid||!t.bid_qty||!t.ask_qty)return std::nullopt;
    if(q>=c.hard_inventory||q<=-c.hard_inventory)return std::nullopt;
    // long double avoids integer multiplication overflow. Replay determinism is
    // guaranteed for the same binary/platform; cross-platform bit identity is not.
    const long double micro=(static_cast<long double>(t.ask)*t.bid_qty+
        static_cast<long double>(t.bid)*t.ask_qty)/(static_cast<long double>(t.bid_qty)+t.ask_qty);
    const auto skew=static_cast<long double>(c.max_inventory_skew)*q/c.hard_inventory;
    const auto fair=micro-skew;
    const auto half=std::max({static_cast<long double>(c.min_half_spread),
                             static_cast<long double>(t.ask-t.bid)/2,
                             static_cast<long double>(cost_floor)});
    // Keep the research quotes passive against this venue's visible BBO. This
    // is not an NBBO or exchange post-only guarantee.
    const auto bid=std::min(std::floor((fair-half)/c.tick)*c.tick,
        std::floor(static_cast<long double>(t.ask-1)/c.tick)*c.tick);
    const auto ask=std::max(std::ceil((fair+half)/c.tick)*c.tick,
        std::ceil((static_cast<long double>(t.bid)+1)/c.tick)*c.tick);
    if(bid<=0||ask>=static_cast<long double>(INT64_MAX)||bid>=ask)return std::nullopt;
    const auto buy=std::min<std::int64_t>(c.quote_size,c.hard_inventory-q);
    const auto sell=std::min<std::int64_t>(c.quote_size,c.hard_inventory+q);
    return Quote{static_cast<Price>(bid),static_cast<Price>(ask),static_cast<Qty>(buy),static_cast<Qty>(sell)};
}
}
