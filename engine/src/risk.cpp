#include "trading/risk.hpp"
#include <algorithm>
#include <limits>

namespace trading {
TokenBucket::TokenBucket(std::uint64_t burst,std::uint64_t rate,std::uint64_t now)
    :capacity_(0),credit_(0),rate_(rate),last_(now) {
    if(burst>UINT64_MAX/unit) throw ProtocolError("token bucket capacity overflow");
    capacity_=credit_=burst*unit;
}
bool TokenBucket::refill(std::uint64_t now) noexcept {
    if(now<last_)return false;
    const auto elapsed=now-last_;last_=now;
    if(!rate_||credit_==capacity_)return true;
    const auto missing=capacity_-credit_;
    const auto duration=missing/rate_+(missing%rate_!=0);
    if(elapsed>=duration) credit_=capacity_;
    else credit_+=elapsed*rate_;
    return true;
}
bool TokenBucket::available(std::uint64_t cost) const noexcept {return credit_/unit>=cost;}
bool TokenBucket::take(std::uint64_t cost) noexcept {
    if(!available(cost))return false;
    credit_-=cost*unit;
    return true;
}
RiskEngine::RiskEngine(RiskLimits limits,std::uint64_t now):limits_(limits),orders_rate_(limits.orders_per_second,limits.orders_per_second,now) {
    if(!limits.max_order||limits.max_position<=0||limits.max_position>1'000'000'000||
       limits.max_open_orders==0||limits.max_open_orders>1'000'000||limits.price_low<=0||
       limits.price_high<limits.price_low||limits.daily_loss<=0||limits.daily_drawdown<=0||
       limits.orders_per_second==0||limits.orders_per_second>1'000'000)
        throw ProtocolError("invalid risk limits");
}
bool RiskEngine::arm_after_reconciliation() noexcept {
    if(killed_||!pnl_.mark_valid)return false;
    armed_=true;
    return true;
}
RiskReject RiskEngine::check_and_reserve(const OrderRequest& r,const RiskContext& c) {
    if(killed_||!armed_)return RiskReject::Disabled;
    if(!c.market_valid||!c.broker_healthy||!c.system_healthy||!c.trading)return RiskReject::Unhealthy;
    if(c.now_ns<c.market_ns||c.now_ns-c.market_ns>limits_.max_market_age_ns)return RiskReject::StaleMarket;
    if(!r.id||!r.quantity||r.quantity>limits_.max_order||r.price<limits_.price_low||r.price>limits_.price_high||
       (r.side!=Side::Buy&&r.side!=Side::Sell))return RiskReject::BadRequest;
    if(orders_.contains(r.id))return RiskReject::Duplicate;
    const auto buys=buys_+(r.side==Side::Buy?r.quantity:0U);
    const auto sells=sells_+(r.side==Side::Sell?r.quantity:0U);
    const auto high=position_+static_cast<std::int64_t>(buys);
    const auto low=position_-static_cast<std::int64_t>(sells);
    if(high>limits_.max_position||low < -limits_.max_position)return RiskReject::Inventory;
    const auto absmax=static_cast<std::uint64_t>(std::max(high<0?-high:high,low<0?-low:low));
    // Conservative configured maximum reference price, rather than the new
    // order's potentially cheaper limit, marks all worst-case position.
    if(absmax>limits_.max_notional/static_cast<std::uint64_t>(limits_.price_high))return RiskReject::Notional;
    if(!pnl_.mark_valid)return RiskReject::Loss;
    if(open_count_>=limits_.max_open_orders||orders_.size()>=1'000'000)return RiskReject::Capacity;
    if(!orders_rate_.refill(c.now_ns)||!orders_rate_.available())return RiskReject::Throttled;
    // Allocation can throw before any exposure/tokens are mutated. This is the
    // correctness/reference model, not a zero-allocation execution hot path.
    orders_.emplace(r.id,Reservation{r,r.quantity,LocalOrderState::PendingNew,true});
    orders_rate_.take();buys_=buys;sells_=sells;++open_count_;return RiskReject::None;
}
bool RiskEngine::acknowledge(RefId id) {
    const auto it=orders_.find(id);if(it==orders_.end())return false;
    if(it->second.state==LocalOrderState::PendingNew)it->second.state=LocalOrderState::Live;
    return true;
}
bool RiskEngine::request_cancel(RefId id) {
    const auto it=orders_.find(id);if(it==orders_.end()||!it->second.reserved)return false;
    it->second.state=LocalOrderState::PendingCancel;return true;
}
void RiskEngine::release(Reservation& o) noexcept {
    if(!o.reserved)return;
    auto& qty=o.request.side==Side::Buy?buys_:sells_;qty-=o.leaves;o.reserved=false;--open_count_;
}
bool RiskEngine::confirm_terminal(RefId id,LocalOrderState terminal) {
    if(terminal!=LocalOrderState::Canceled&&terminal!=LocalOrderState::Rejected)return false;
    const auto it=orders_.find(id);if(it==orders_.end())return false;
    if(it->second.state==LocalOrderState::Filled)return true;
    release(it->second);it->second.state=terminal;return true;
}
bool RiskEngine::mark_unknown(RefId id) {
    const auto it=orders_.find(id);if(it==orders_.end()||!it->second.reserved)return false;
    it->second.state=LocalOrderState::Unknown;return true;
}
bool RiskEngine::fill(std::uint64_t execution_id,RefId id,Qty quantity,Price price) {
    if(!execution_id||!quantity||price<=0)throw ProtocolError("invalid execution");
    if(const auto known=executions_.find(execution_id);known!=executions_.end()) {
        if(known->second.order!=id||known->second.quantity!=quantity||known->second.price!=price)
            throw ProtocolError("conflicting execution identity");
        return false;
    }
    auto it=orders_.find(id);
    if(it==orders_.end()||quantity>it->second.leaves)throw ProtocolError("unknown or overfilled order");
    if(executions_.size()>=2'000'000)throw ProtocolError("execution dedup capacity exhausted");
    executions_.emplace(execution_id,Fill{id,quantity,price});
    auto& o=it->second;
    const auto signed_qty=static_cast<std::int64_t>(quantity)*(o.request.side==Side::Buy?1:-1);
    position_+=signed_qty;
    if(o.reserved){auto& qty=o.request.side==Side::Buy?buys_:sells_;qty-=quantity;}
    o.leaves-=quantity;
    if(o.leaves==0){if(o.reserved)--open_count_;o.reserved=false;o.state=LocalOrderState::Filled;}
    else if(o.reserved&&o.state!=LocalOrderState::PendingCancel&&o.state!=LocalOrderState::Unknown)
        o.state=LocalOrderState::Partial;
    // A legal late fill after cancel confirmation still changes position. A
    // resulting breach latches kill; it must never be discarded as "terminal".
    const auto high=max_position();
    const auto low=min_position();
    const auto absmax=static_cast<std::uint64_t>(std::max(high<0?-high:high,low<0?-low:low));
    // A late fill can leave the position-size limit intact yet breach a tighter
    // notional cap after replacement exposure was reserved. Recheck the same
    // conservative price bound used at admission, without multiplying/overflow.
    if(high>limits_.max_position||low < -limits_.max_position||
       absmax>limits_.max_notional/static_cast<std::uint64_t>(limits_.price_high))kill();
    return true;
}
static std::int64_t checked_add(std::int64_t a,std::int64_t b) {
    if((b>0&&a>INT64_MAX-b)||(b<0&&a<INT64_MIN-b))throw ProtocolError("PnL overflow");
    return a+b;
}
void RiskEngine::update_pnl(std::int64_t realized,std::int64_t unrealized,std::int64_t fees,std::int64_t rebates,bool valid) {
    if(fees<0||rebates<0)throw ProtocolError("negative fees or rebates");
    const auto net=checked_add(checked_add(checked_add(realized,unrealized),-fees),rebates);
    pnl_.realized=realized;pnl_.unrealized=unrealized;pnl_.fees=fees;pnl_.rebates=rebates;pnl_.net=net;pnl_.mark_valid=valid;
    pnl_.high_watermark=std::max(pnl_.high_watermark,net);
    if(!valid){armed_=false;return;}
    // Compare without subtracting potentially extreme signed PnL values.
    const bool drawdown=net<=INT64_MAX-limits_.daily_drawdown&&pnl_.high_watermark>=net+limits_.daily_drawdown;
    if(net<=-limits_.daily_loss||drawdown)kill();
}
}
