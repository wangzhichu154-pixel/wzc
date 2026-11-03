#include "trading/vector_core.h"
#include "trading/book.hpp"
#include "trading/risk.hpp"
#include "trading/strategy.hpp"
#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <map>
#include <memory>
#include <thread>
#include <unordered_set>

namespace {
trading::RiskLimits limits(const vc_config& c) {
    trading::RiskLimits r;
    r.max_order=c.max_order_size;r.max_position=c.hard_inventory_limit;
    r.max_notional=c.max_notional_units;r.max_open_orders=static_cast<std::size_t>(c.max_open_orders);
    r.daily_loss=c.daily_loss_limit_units;r.daily_drawdown=c.daily_drawdown_limit_units;
    r.max_market_age_ns=c.max_market_age_ns;r.orders_per_second=c.orders_per_second;
    r.price_low=c.price_low;r.price_high=c.price_high;return r;
}
bool boolean(uint8_t value){return value<=1;}
}

struct vc_handle {
    vc_config config;
    trading::Book book;
    trading::RiskEngine risk;
    trading::PricingConfig pricing;
    vc_health health{};
    std::thread::id owner{std::this_thread::get_id()};
    std::map<uint64_t,vc_permit> permits;
    std::unordered_set<uint64_t> send_attempted;
    std::array<char,256> error{};
    trading::Top previous_top;
    uint64_t last_input_ns{},last_bbo_ns{},last_clock_ns{};
    uint32_t reason{VC_FAULT_NONE},last_reject{};
    bool input_seen{false},bbo_seen{false},pnl_received{false},reconcile_required{false};
    bool quote_economics_configured{false},quote_economics_frozen{false};
    explicit vc_handle(const vc_config& c):config(c),book(static_cast<std::size_t>(c.max_book_orders),static_cast<std::size_t>(c.max_trade_records)),risk(limits(c)) {
        pricing.tick=c.tick_size;pricing.min_half_spread=c.tick_size;
        pricing.max_inventory_skew=c.tick_size*5;pricing.hard_inventory=c.hard_inventory_limit;pricing.quote_size=c.max_order_size;
    }
};

namespace {
int fail(vc_handle* h,int result,const char* message) noexcept {
    if(h)std::snprintf(h->error.data(),h->error.size(),"%s",message);
    return result;
}
void trip(vc_handle* h,uint32_t reason) noexcept {
    h->risk.kill();h->reconcile_required=true;
    if(h->reason==VC_FAULT_NONE)h->reason=reason;
}
template<class F>int guarded(vc_handle* h,F&& operation) noexcept {
    if(!h||h->owner!=std::this_thread::get_id())return VC_INVALID_ARGUMENT;
    try {return operation();}
    catch(const trading::ProtocolError& error){trip(h,VC_FAULT_PROTOCOL);return fail(h,VC_RECONCILIATION_REQUIRED,error.what());}
    catch(const std::exception& error){trip(h,VC_FAULT_SYSTEM);return fail(h,VC_INTERNAL_ERROR,error.what());}
    catch(...){trip(h,VC_FAULT_SYSTEM);return fail(h,VC_INTERNAL_ERROR,"unknown C++ failure");}
}
bool clock_ok(vc_handle* h,uint64_t now) noexcept {
    if(now<h->last_clock_ns){trip(h,VC_FAULT_CLOCK);fail(h,VC_RECONCILIATION_REQUIRED,"monotonic clock regressed");return false;}
    h->last_clock_ns=now;return true;
}
bool fresh(const vc_handle* h,uint64_t now) noexcept {
    return h->bbo_seen&&now>=h->last_bbo_ns&&now-h->last_bbo_ns<=h->config.max_market_age_ns&&
        h->health.live_source&&h->health.feed_connected&&h->health.sequence_complete&&
        h->book.tradable(static_cast<std::uint16_t>(h->config.symbol_locate));
}
bool ready(const vc_handle* h,uint64_t now) noexcept {
    return !h->risk.killed()&&!h->reconcile_required&&h->health.broker_healthy&&
        h->health.system_healthy&&h->pnl_received&&h->risk.pnl().mark_valid&&fresh(h,now)&&h->quote_economics_configured;
}
bool passive(const vc_handle* h,uint8_t side,int64_t price) noexcept {
    const auto top=h->book.top(static_cast<std::uint16_t>(h->config.symbol_locate));
    return top.valid&&(side==0?price<top.ask:price>top.bid);
}
int terminal(vc_handle* h,uint64_t id,trading::LocalOrderState state) {
    if(!h->risk.confirm_terminal(id,state)) {
        trip(h,VC_FAULT_UNKNOWN_ORDER);return fail(h,VC_RECONCILIATION_REQUIRED,"terminal report for unknown order");
    }
    return VC_OK;
}
}

extern "C" int vc_create(const vc_config* c,vc_handle** output) {
    if(!output)return VC_INVALID_ARGUMENT;
    *output=nullptr;
    if(!c||c->abi_version!=VC_ABI_VERSION||c->struct_size!=sizeof(vc_config)||
       c->symbol_locate==0||c->symbol_locate>65535||c->max_order_size==0||c->max_order_size>1'000'000||
       c->max_open_orders==0||c->max_open_orders>1'000'000||c->max_notional_units==0||
       c->max_market_age_ns==0||c->max_market_age_ns>60'000'000'000ULL||
       c->tick_size<=0||c->tick_size>1'000'000'000||
       c->max_book_orders==0||c->max_book_orders>10'000'000||
       c->max_trade_records==0||c->max_trade_records>10'000'000||
       c->symbol[0]=='\0'||c->symbol[8]!='\0')return VC_INVALID_ARGUMENT;
    for(size_t i=0;i<8&&c->symbol[i]!='\0';++i)if(c->symbol[i]<33||c->symbol[i]>126)return VC_INVALID_ARGUMENT;
    try {*output=new vc_handle(*c);return VC_OK;}
    catch(const trading::ProtocolError&){return VC_INVALID_ARGUMENT;}
    catch(...){return VC_INTERNAL_ERROR;}
}
extern "C" int vc_destroy(vc_handle* h) {
    if(!h)return VC_OK;
    if(h->owner!=std::this_thread::get_id())return VC_INVALID_ARGUMENT;
    try{delete h;return VC_OK;}catch(...){return VC_INTERNAL_ERROR;}
}
extern "C" int vc_last_error(vc_handle* h,char* output,uint32_t capacity) {
    return guarded(h,[&]()->int{if(!output||capacity==0)return VC_INVALID_ARGUMENT;
        std::snprintf(output,capacity,"%s",h->error.data());return VC_OK;});
}
extern "C" int vc_set_quote_economics(vc_handle* h,int64_t fee,int64_t minimum,int64_t slippage,int64_t edge) {
    return guarded(h,[&]()->int{
        if(h->risk.armed()||h->quote_economics_frozen||h->risk.open_order_count()!=0)
            return fail(h,VC_NOT_READY,"quote economics are immutable after first arm or while orders are reserved");
        auto candidate=h->pricing;
        candidate.fee_per_share_units=fee;candidate.min_fee_per_order_units=minimum;
        candidate.slippage_buffer_units=slippage;candidate.min_net_edge_units=edge;
        try{(void)trading::quote_cost_floor(candidate);}
        catch(const trading::ProtocolError&){return fail(h,VC_INVALID_ARGUMENT,"each quote economic component must be 0..1e9 units, sum <=4e9");}
        h->pricing=candidate;h->quote_economics_configured=true;return VC_OK;
    });
}
extern "C" int vc_process_itch(vc_handle* h,const uint8_t* bytes,uint32_t size,uint64_t received) {
    return guarded(h,[&]()->int{
        if(!bytes)return fail(h,VC_INVALID_ARGUMENT,"null raw ITCH buffer");
        if(size<11||size>50)throw trading::ProtocolError("invalid raw ITCH message length");
        // RX timestamps can legitimately precede a later control-loop clock
        // sample. Only regressions within the ordered RX stream are faults.
        if(h->input_seen&&received<h->last_input_ns){trip(h,VC_FAULT_CLOCK);return VC_RECONCILIATION_REQUIRED;}
        h->last_clock_ns=std::max(h->last_clock_ns,received);
        const auto event=trading::decode_itch(std::span<const std::byte>(reinterpret_cast<const std::byte*>(bytes),size));
        h->last_input_ns=received;h->input_seen=true;
        if(event.type!='S'&&event.locate!=h->config.symbol_locate)return VC_OK;
        const auto name=trading::stock_name(event.stock);
        if(!name.empty()&&name!=std::string(h->config.symbol))throw trading::ProtocolError("configured stock locate does not match symbol");
        h->book.apply(event);
        const auto top=h->book.top(static_cast<std::uint16_t>(h->config.symbol_locate));
        const bool changed=top.bid!=h->previous_top.bid||top.ask!=h->previous_top.ask||top.bid_qty!=h->previous_top.bid_qty||top.ask_qty!=h->previous_top.ask_qty||top.valid!=h->previous_top.valid;
        const bool mutation=event.type=='A'||event.type=='F'||event.type=='X'||event.type=='D'||event.type=='U'||event.type=='E'||event.type=='C';
        if(mutation&&changed&&top.valid&&h->book.tradable(static_cast<std::uint16_t>(h->config.symbol_locate))){h->last_bbo_ns=received;h->bbo_seen=true;}
        h->previous_top=top;
        if(h->risk.armed()&&!h->book.tradable(static_cast<std::uint16_t>(h->config.symbol_locate))){
            trip(h,VC_FAULT_FEED);return fail(h,VC_RECONCILIATION_REQUIRED,"working market became invalid or halted");
        }
        return VC_OK;
    });
}
extern "C" int vc_set_health(vc_handle* h,const vc_health* health) {
    return guarded(h,[&]()->int{
        if(!health||!boolean(health->broker_healthy)||!boolean(health->system_healthy)||
           !boolean(health->feed_connected)||!boolean(health->sequence_complete)||!boolean(health->live_source))return VC_INVALID_ARGUMENT;
        if(h->health.broker_healthy&&!health->broker_healthy)trip(h,VC_FAULT_BROKER);
        if((h->health.feed_connected&&!health->feed_connected)||(h->health.sequence_complete&&!health->sequence_complete)||
           (h->health.live_source&&!health->live_source))trip(h,VC_FAULT_FEED);
        if(h->health.system_healthy&&!health->system_healthy)trip(h,VC_FAULT_SYSTEM);
        h->health=*health;return h->reconcile_required?VC_RECONCILIATION_REQUIRED:VC_OK;
    });
}
extern "C" int vc_get_quote(vc_handle* h,uint64_t now,vc_quote* out) {
    return guarded(h,[&]()->int{
        if(!out)return VC_INVALID_ARGUMENT;
        *out={};
        if(!clock_ok(h,now))return VC_RECONCILIATION_REQUIRED;
        const auto top=h->book.top(static_cast<std::uint16_t>(h->config.symbol_locate));
        out->market_bid=top.bid;out->market_ask=top.ask;out->market_bid_quantity=top.bid_qty;out->market_ask_quantity=top.ask_qty;
        out->last_valid_bbo_ns=h->last_bbo_ns;out->market_fresh=static_cast<uint8_t>(fresh(h,now));
        out->trading_status_valid=static_cast<uint8_t>(h->book.tradable(static_cast<std::uint16_t>(h->config.symbol_locate)));
        if(h->risk.armed()&&!out->market_fresh)trip(h,VC_FAULT_FEED);
        if(!out->market_fresh||h->risk.killed()||h->reconcile_required||!h->quote_economics_configured)return VC_OK;
        const auto position=std::clamp(h->risk.position(),-h->config.hard_inventory_limit+1,h->config.hard_inventory_limit-1);
        const auto quote=trading::price_quote(top,position,h->pricing);if(!quote)return VC_OK;
        out->bid_price=quote->bid;out->ask_price=quote->ask;out->bid_quantity=quote->bid_size;out->ask_quantity=quote->ask_size;
        if(h->risk.max_position()>=h->config.hard_inventory_limit)out->bid_quantity=0;
        if(h->risk.min_position()<=-h->config.hard_inventory_limit)out->ask_quantity=0;
        out->quote_available=1;return VC_OK;
    });
}
extern "C" int vc_get_snapshot(vc_handle* h,vc_snapshot* out) {
    return guarded(h,[&]()->int{
        if(!out)return VC_INVALID_ARGUMENT;
        *out={};
        out->risk_epoch=h->risk.epoch();out->position=h->risk.position();
        out->buy_reserved=static_cast<uint64_t>(h->risk.max_position()-h->risk.position());
        out->sell_reserved=static_cast<uint64_t>(h->risk.position()-h->risk.min_position());
        out->open_orders=h->risk.open_order_count();out->net_pnl_units=h->risk.pnl().net;
        out->reason=h->reason;out->last_risk_reject=h->last_reject;
        out->killed=h->risk.killed();out->armed=h->risk.armed();out->reconcile_required=h->reconcile_required;
        out->session_complete=h->book.initialized()&&h->health.sequence_complete;
        out->pnl_mark_valid=h->pnl_received&&h->risk.pnl().mark_valid&&fresh(h,h->last_clock_ns);
        return VC_OK;
    });
}
extern "C" int vc_arm_flat(vc_handle* h,uint8_t paper,uint8_t reconciled,uint64_t open,int64_t position,uint64_t now) {
    return guarded(h,[&]()->int{
        if(!boolean(paper)||!boolean(reconciled))return VC_INVALID_ARGUMENT;
        if(!clock_ok(h,now))return VC_RECONCILIATION_REQUIRED;
        if(h->reconcile_required||h->risk.killed())return fail(h,VC_RECONCILIATION_REQUIRED,"kill cannot be reset through C ABI");
        if(!paper||!reconciled||open!=0||position!=0||h->risk.position()!=0||h->risk.open_order_count()!=0)
            return fail(h,VC_NOT_READY,"paper identity and complete empty/flat reconciliation required");
        if(!ready(h,now)||!h->book.initialized())return fail(h,VC_NOT_READY,"explicit quote economics, complete fresh live session, broker health and reconciled PnL required");
        if(!h->risk.arm_after_reconciliation())return VC_NOT_READY;
        h->quote_economics_frozen=true;return VC_OK;
    });
}
extern "C" int vc_check_and_reserve(vc_handle* h,uint64_t id,uint8_t side,uint32_t quantity,int64_t price,uint64_t now,uint64_t expires,vc_permit* out) {
    return guarded(h,[&]()->int{
        if(!out)return VC_INVALID_ARGUMENT;
        *out={};
        if(!clock_ok(h,now))return VC_RECONCILIATION_REQUIRED;
        if(side>1||expires<=now||price<=0||price%h->config.tick_size!=0)return fail(h,VC_INVALID_ARGUMENT,"invalid order, tick or deadline");
        if(!ready(h,now)||!h->risk.armed())return fail(h,VC_NOT_READY,"risk not armed, health invalid or market/PnL stale");
        if(!passive(h,side,price))return fail(h,VC_RISK_REJECTED,"marketable order rejected by passive paper policy");
        const trading::RiskContext ctx{true,true,true,true,now,h->last_bbo_ns};
        const auto verdict=h->risk.check_and_reserve({id,side==0?trading::Side::Buy:trading::Side::Sell,quantity,price},ctx);
        h->last_reject=static_cast<uint32_t>(verdict);
        if(verdict!=trading::RiskReject::None)return fail(h,VC_RISK_REJECTED,"synchronous pretrade rejection");
        const vc_permit permit{id,h->risk.epoch(),expires};h->permits.emplace(id,permit);*out=permit;return VC_OK;
    });
}
extern "C" int vc_send_boundary(vc_handle* h,const vc_permit* p,uint64_t now) {
    return guarded(h,[&]()->int{
        if(!p)return VC_INVALID_ARGUMENT;
        if(!clock_ok(h,now))return VC_RECONCILIATION_REQUIRED;
        const auto known=h->permits.find(p->reservation_id);
        if(known==h->permits.end()||known->second.risk_epoch!=p->risk_epoch||known->second.deadline_ns!=p->deadline_ns)
            return fail(h,VC_INVALID_ARGUMENT,"unknown or modified risk permit");
        if(h->send_attempted.contains(p->reservation_id))return fail(h,VC_DUPLICATE,"send permit already consumed");
        if(!ready(h,now)||!h->risk.armed()||p->risk_epoch!=h->risk.epoch()||now>=p->deadline_ns)
            return fail(h,VC_NOT_READY,"permit expired or safety state changed");
        const auto& order=h->risk.orders().at(p->reservation_id);
        if(!order.reserved||order.state!=trading::LocalOrderState::PendingNew||
           !passive(h,order.request.side==trading::Side::Buy?0:1,order.request.price))return VC_NOT_READY;
        h->send_attempted.insert(p->reservation_id);return VC_OK;
    });
}
extern "C" int vc_ack(vc_handle* h,uint64_t id) {
    return guarded(h,[&]()->int{if(!h->send_attempted.contains(id)||!h->risk.acknowledge(id)){trip(h,VC_FAULT_UNKNOWN_ORDER);return VC_RECONCILIATION_REQUIRED;}return VC_OK;});
}
extern "C" int vc_fill(vc_handle* h,uint64_t execution,uint64_t id,uint32_t quantity,int64_t price) {
    return guarded(h,[&]()->int{
        if(!h->send_attempted.contains(id)){trip(h,VC_FAULT_UNKNOWN_ORDER);return fail(h,VC_RECONCILIATION_REQUIRED,"execution has no known dispatch");}
        const auto fresh_fill=h->risk.fill(execution,id,quantity,price);
        if(fresh_fill)h->pnl_received=false;
        if(h->risk.killed())trip(h,VC_FAULT_LOSS);
        return fresh_fill?VC_OK:VC_DUPLICATE;
    });
}
extern "C" int vc_cancel_request(vc_handle* h,uint64_t id) {
    return guarded(h,[&]()->int{return h->risk.request_cancel(id)?VC_OK:VC_NOT_READY;});
}
extern "C" int vc_cancel_confirm(vc_handle* h,uint64_t id) {
    return guarded(h,[&]()->int{return terminal(h,id,trading::LocalOrderState::Canceled);});
}
extern "C" int vc_reject(vc_handle* h,uint64_t id) {
    return guarded(h,[&]()->int{return terminal(h,id,trading::LocalOrderState::Rejected);});
}
extern "C" int vc_definitely_not_sent(vc_handle* h,uint64_t id) {return vc_reject(h,id);}
extern "C" int vc_outcome_unknown(vc_handle* h,uint64_t id) {
    return guarded(h,[&]()->int{h->risk.mark_unknown(id);trip(h,VC_FAULT_UNKNOWN_ORDER);return VC_RECONCILIATION_REQUIRED;});
}
extern "C" int vc_execution_correction(vc_handle* h) {
    return guarded(h,[&]()->int{trip(h,VC_FAULT_EXECUTION_CORRECTION);return fail(h,VC_RECONCILIATION_REQUIRED,"execution correction requires full external ledger reconciliation");});
}
extern "C" int vc_update_pnl(vc_handle* h,int64_t realized,int64_t unrealized,int64_t fees,int64_t rebates,uint8_t valid) {
    return guarded(h,[&]()->int{
        if(!boolean(valid))return VC_INVALID_ARGUMENT;
        const bool was_armed=h->risk.armed();h->risk.update_pnl(realized,unrealized,fees,rebates,valid!=0);
        h->pnl_received=true;
        if(was_armed&&!valid)trip(h,VC_FAULT_FEED);
        if(h->risk.killed())trip(h,VC_FAULT_LOSS);
        return h->reconcile_required?VC_RECONCILIATION_REQUIRED:VC_OK;
    });
}
extern "C" int vc_kill(vc_handle* h,uint32_t reason) {
    return guarded(h,[&]()->int{if(reason<VC_FAULT_MANUAL||reason>VC_FAULT_SYSTEM)return VC_INVALID_ARGUMENT;trip(h,reason);return VC_OK;});
}
