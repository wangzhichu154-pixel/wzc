#include "trading/simulation.hpp"
#include <algorithm>
#include <limits>
#include <iomanip>
#include <sstream>

namespace trading {
namespace {
std::int64_t add_checked(std::int64_t a,std::int64_t b) {
    if((b>0&&a>INT64_MAX-b)||(b<0&&a<INT64_MIN-b))throw ProtocolError("simulation accounting overflow");
    return a+b;
}
std::int64_t mul_checked(std::int64_t price,std::uint64_t quantity) {
    if(price==INT64_MIN)throw ProtocolError("simulation invalid numeric range");
    const auto absolute=static_cast<std::uint64_t>(price<0?-price:price);
    if(quantity&&absolute>static_cast<std::uint64_t>(INT64_MAX)/quantity)throw ProtocolError("simulation notional overflow");
    const auto value=static_cast<std::int64_t>(absolute*quantity);return price<0?-value:value;
}
std::uint64_t deadline(std::uint64_t now,std::uint64_t delay) {
    if(delay>UINT64_MAX-now)throw ProtocolError("simulation timer overflow");
    return now+delay;
}
RiskLimits make_limits(const SimulationConfig& c) {
    RiskLimits l;l.max_order=c.max_order_size;l.max_position=c.hard_inventory_limit;
    l.max_open_orders=2;l.orders_per_second=c.orders_per_second;
    l.daily_loss=c.daily_loss_limit_units;l.daily_drawdown=c.daily_loss_limit_units;
    l.price_high=UINT32_MAX;
    if(c.hard_inventory_limit<=0||c.hard_inventory_limit>1'000'000'000)
        throw ProtocolError("invalid simulation inventory limit");
    l.max_notional=static_cast<std::uint64_t>(c.hard_inventory_limit)*UINT32_MAX;
    l.max_market_age_ns=c.quote_lifetime_ns;return l;
}
void hash_value(std::uint64_t& h,std::uint64_t value) {
    for(int i=0;i<8;++i){h^=value&255U;h*=1099511628211ULL;value>>=8;}
}
std::uint64_t add_unsigned(std::uint64_t a,std::uint64_t b) {
    if(b>UINT64_MAX-a)throw ProtocolError("simulation metric overflow");
    return a+b;
}
}
Simulator::Simulator(SimulationConfig c):config_(c),risk_(make_limits(c)) {
    if(c.soft_inventory_limit<=0||c.soft_inventory_limit>=c.hard_inventory_limit||
       c.max_order_size==0||c.max_order_size>1'000'000||c.max_order_size>c.hard_inventory_limit||
       c.tick_size<=0||c.tick_size>1'000'000'000||
       c.fee_per_share_units<0||c.rebate_per_share_units<0||c.quote_lifetime_ns==0||
       c.fee_per_share_units>max_quote_economic_component_units||c.rebate_per_share_units>1'000'000||
       c.latency_ns>60'000'000'000ULL||c.cancel_latency_ns>60'000'000'000ULL||
       c.quote_lifetime_ns>3'600'000'000'000ULL||
       c.trade_start_ns>=c.trade_end_ns||c.trade_end_ns>86'400'000'000'000ULL||
       c.max_signal_age_ns==0||c.max_signal_age_ns>3'600'000'000'000ULL)
        throw ProtocolError("invalid simulation configuration");
    pricing_.tick=c.tick_size;pricing_.min_half_spread=c.tick_size;
    pricing_.max_inventory_skew=c.tick_size*5;
    pricing_.hard_inventory=c.hard_inventory_limit;pricing_.quote_size=c.max_order_size;
    pricing_.fee_per_share_units=c.fee_per_share_units;
    pricing_.min_fee_per_order_units=c.min_fee_per_order_units;
    pricing_.slippage_buffer_units=c.slippage_buffer_units;
    pricing_.min_net_edge_units=c.min_net_edge_units;
    (void)quote_cost_floor(pricing_);
    risk_.update_pnl(0,0,0,0,true);
}
void Simulator::cancel(std::size_t side) {
    cancel_at(side,now_);
}
void Simulator::cancel_at(std::size_t side,std::uint64_t request_time) {
    if(!working_[side]||working_[side]->cancel_pending)return;
    auto& order=*working_[side];
    order.cancel_pending=true;order.cancel_after_ns=deadline(request_time,config_.cancel_latency_ns);
    if(!risk_.request_cancel(order.id))throw ProtocolError("simulation cancel/OMS mismatch");
    ++stats_.cancel_requests;
}
void Simulator::account_fill(RefId order_id,Side side,Qty quantity,Price price) {
    if(quantity==0)throw ProtocolError("zero-quantity modeled fill cannot incur a commission");
    if(!order_fees_.contains(order_id)&&order_fees_.size()>=1'000'000)
        throw ProtocolError("simulation order-fee ledger capacity exhausted");
    auto& order_fee=order_fees_[order_id];
    if(quantity>UINT64_MAX-order_fee.filled_quantity)
        throw ProtocolError("simulation cumulative fill overflow");
    const auto cumulative=order_fee.filled_quantity+quantity;
    const auto total_fee=std::max(config_.min_fee_per_order_units,mul_checked(config_.fee_per_share_units,cumulative));
    if(total_fee<order_fee.charged_units)throw ProtocolError("simulation commission decreased without correction");
    const auto incremental_fee=total_fee-order_fee.charged_units;
    const auto next_fees=add_checked(stats_.fees,incremental_fee);
    const auto next_rebates=add_checked(stats_.rebates,mul_checked(config_.rebate_per_share_units,quantity));
    const auto signed_qty=static_cast<std::int64_t>(quantity)*(side==Side::Buy?1:-1);
    const auto current_position=risk_.position();
    if(config_.long_only&&current_position<0)throw ProtocolError("long-only simulation inventory invariant failed");
    const auto prior_position=current_position-signed_qty;
    const bool direction_flip=prior_position!=0&&current_position!=0&&((prior_position>0)!=(current_position>0));
    if(prior_position!=0&&(current_position==0||direction_flip)) {
        if(!holding_start_ns_||now_<*holding_start_ns_)throw ProtocolError("simulation holding-time state mismatch");
        const auto elapsed=now_-*holding_start_ns_;
        stats_.holding_total_ns=add_unsigned(stats_.holding_total_ns,elapsed);
        stats_.holding_max_ns=std::max(stats_.holding_max_ns,elapsed);
        ++stats_.closed_inventory_episodes;holding_start_ns_.reset();
    }
    if(current_position!=0&&(prior_position==0||direction_flip))holding_start_ns_=now_;
    const auto absolute_position=static_cast<std::uint64_t>(current_position<0?-current_position:current_position);
    stats_.peak_abs_inventory=std::max(stats_.peak_abs_inventory,absolute_position);
    if(now_>=config_.trade_start_ns&&now_<config_.trade_end_ns)
        stats_.peak_abs_inventory_in_window=std::max(stats_.peak_abs_inventory_in_window,absolute_position);
    auto remaining=signed_qty;
    while(remaining!=0&&!lots_.empty()&&((remaining>0)!=(lots_.front().quantity>0))) {
        auto& lot=lots_.front();
        const auto count=static_cast<std::uint64_t>(std::min(remaining>0?remaining:-remaining,lot.quantity>0?lot.quantity:-lot.quantity));
        const auto profit=lot.quantity>0?price-lot.price:lot.price-price;
        stats_.realized=add_checked(stats_.realized,mul_checked(profit,count));
        lot.quantity+=lot.quantity>0?-static_cast<std::int64_t>(count):static_cast<std::int64_t>(count);
        remaining+=remaining>0?-static_cast<std::int64_t>(count):static_cast<std::int64_t>(count);
        if(lot.quantity==0)lots_.pop_front();
    }
    if(remaining)lots_.push_back({remaining,price});
    const auto notional=mul_checked(price,quantity);
    auto& side_quantity=side==Side::Buy?stats_.buy_quantity:stats_.sell_quantity;
    auto& side_notional=side==Side::Buy?stats_.buy_notional:stats_.sell_notional;
    side_quantity=add_unsigned(side_quantity,quantity);
    side_notional=add_unsigned(side_notional,static_cast<std::uint64_t>(notional));
    stats_.turnover=add_unsigned(stats_.turnover,static_cast<std::uint64_t>(notional));
    if(config_.participation_gate_required&&now_>=config_.trade_end_ns) {
        ++stats_.fills_after_window_end;
        stats_.quantity_after_window_end=add_unsigned(stats_.quantity_after_window_end,quantity);
        stats_.turnover_after_window_end=add_unsigned(stats_.turnover_after_window_end,static_cast<std::uint64_t>(notional));
    }
    stats_.cash=add_checked(stats_.cash,side==Side::Buy?-notional:notional);
    stats_.fees=next_fees;
    stats_.rebates=next_rebates;
    order_fee.filled_quantity=cumulative;order_fee.charged_units=total_fee;
    ++stats_.fill_count;stats_.filled_quantity+=quantity;
}
void Simulator::before_event(const Event& event,const Book& book,std::uint64_t time,std::uint64_t sequence) {
    if(sequence<=sequence_||time<now_)throw ProtocolError("simulation event ordering violation");
    // An incoming book update (or flattening fill) must not erase the interval
    // for which the previously held inventory had no sufficiently fresh mark.
    if(risk_.position()!=0&&(!stats_.pnl_mark_valid||!last_valid_bbo_ns_||
       time<*last_valid_bbo_ns_||time-*last_valid_bbo_ns_>config_.quote_lifetime_ns))
        valuation_path_complete_=false;
    if(!first_event_ns_)first_event_ns_=time;
    else {
        const auto begin=std::max(now_,config_.trade_start_ns);
        const auto end=std::min(time,config_.trade_end_ns);
        if(end>begin) {
            stats_.window_observed_ns=add_unsigned(stats_.window_observed_ns,end-begin);
            const auto position=risk_.position();
            inventory_share_ns_+=static_cast<long double>(position<0?-position:position)*static_cast<long double>(end-begin);
        }
    }
    if(config_.participation_gate_required&&!window_end_observed_&&time>=config_.trade_end_ns) {
        inventory_at_window_end_=risk_.position();window_end_observed_=true;
    }
    now_=time;sequence_=sequence;
    if(event.type=='B'&&fill_evidence_matches_.contains(event.match))
        throw ProtocolError("simulation broken trade invalidates fill evidence; execution correction model required");
    if(event.locate!=0) {
        if(symbol_&&*symbol_!=event.locate)throw ProtocolError("simulation requires a single stock locate; multi-symbol capture rejected");
        symbol_=event.locate;
    }
    for(auto& slot:working_) {
        if(!slot)continue;
        auto& o=*slot;
        if(config_.participation_gate_required) {
            std::optional<std::uint64_t> revoke_at;
            if(gate_seen_&&gate_expires_ns_<=now_)revoke_at=gate_expires_ns_;
            if(config_.trade_end_ns<=now_)revoke_at=revoke_at?std::min(*revoke_at,config_.trade_end_ns):config_.trade_end_ns;
            if(revoke_at&&!o.cancel_pending)cancel_at(o.side==Side::Buy?0:1,*revoke_at);
        }
        const auto expiry=deadline(o.submitted_ns,config_.quote_lifetime_ns);
        if(!o.cancel_pending&&expiry<=now_)cancel_at(o.side==Side::Buy?0:1,expiry);
        // Strict inequality handles ambiguous same-timestamp races in the
        // conservative direction: a cancel is not assumed effective yet.
        if(o.cancel_pending&&o.cancel_after_ns<now_) {
            if(!risk_.confirm_terminal(o.id,LocalOrderState::Canceled))throw ProtocolError("simulation cancel terminal mismatch");
            ++stats_.orders_canceled;slot.reset();continue;
        }
        if(!o.active&&o.active_after_ns<now_) {
            if(!symbol_||!book.tradable(*symbol_)) { cancel(o.side==Side::Buy?0:1);continue; }
            const auto top=book.top(*symbol_);
            if((o.side==Side::Buy&&o.price>=top.ask)||(o.side==Side::Sell&&o.price<=top.bid)) {
                if(!risk_.confirm_terminal(o.id,LocalOrderState::Rejected))throw ProtocolError("simulation reject/OMS mismatch");
                ++stats_.post_only_rejections;slot.reset();continue;
            }
            o.active=true;o.activation_sequence=sequence_;++stats_.orders_activated;
            for(const auto& [id,qty]:book.queue_at(*symbol_,o.side,o.price)){o.ahead.emplace(id,qty);o.ahead_quantity+=qty;}
            if(!risk_.acknowledge(o.id))throw ProtocolError("simulation acknowledgement mismatch");
        }
    }
    if(event.type!='E'&&event.type!='C')return;
    const auto resting=book.order(event.reference);
    if(!resting)return; // Book::apply will report the actual integrity fault.
    if(event.locate!=resting->locate||event.quantity>resting->leaves)
        throw ProtocolError("simulation invalid public execution");
    const auto index=resting->side==Side::Buy?0U:1U;
    auto& slot=working_[index];
    if(!slot||!slot->active||slot->price!=resting->price)return;
    auto& o=*slot;
    if(auto ahead=o.ahead.find(event.reference);ahead!=o.ahead.end()) {
        const auto consumed=std::min(event.quantity,ahead->second);
        ahead->second-=consumed;o.ahead_quantity-=consumed;stats_.queue_consumed+=consumed;
        if(ahead->second==0)o.ahead.erase(ahead);
        return; // An execution of known queue-ahead shares is never our fill.
    }
    if(o.ahead_quantity!=0||sequence_<=o.activation_sequence||
       (event.type=='C'&&event.price!=o.price))return;
    const auto qty=static_cast<Qty>(std::min<std::uint64_t>(o.leaves,event.quantity));
    if(qty==0)return;
    fill_evidence_matches_.insert(event.match);
    if(o.cancel_pending)++stats_.fills_while_cancel_pending;
    // Generate a separate, namespaced synthetic execution identity only after
    // queue, price, activation and subsequent-trade eligibility are established.
    if(!risk_.fill(++execution_id_,o.id,qty,o.price))throw ProtocolError("simulation duplicate generated execution");
    account_fill(o.id,o.side,qty,o.price);o.leaves-=qty;
    hash_value(decision_hash_,execution_id_);hash_value(decision_hash_,o.id);hash_value(decision_hash_,qty);hash_value(decision_hash_,static_cast<std::uint64_t>(o.price));
    if(o.leaves==0)slot.reset();
}
void Simulator::mark(const Event& event,const Book& book) {
    const auto top=symbol_?book.top(*symbol_):Top{};
    const bool book_event=event.type=='A'||event.type=='F'||event.type=='X'||event.type=='D'||event.type=='U'||event.type=='E'||event.type=='C';
    const bool changed=top.bid!=previous_top_.bid||top.ask!=previous_top_.ask||top.bid_qty!=previous_top_.bid_qty||top.ask_qty!=previous_top_.ask_qty||top.valid!=previous_top_.valid;
    if(book_event&&changed&&top.valid&&symbol_&&book.tradable(*symbol_))last_valid_bbo_ns_=now_;
    previous_top_=top;
    const bool fresh=last_valid_bbo_ns_&&now_>=*last_valid_bbo_ns_&&now_-*last_valid_bbo_ns_<=config_.quote_lifetime_ns;
    stats_.pnl_mark_valid=top.valid&&fresh&&symbol_&&book.tradable(*symbol_);
    if(stats_.pnl_mark_valid) {
        std::int64_t unrealized=0,inventory=0;
        for(const auto& lot:lots_) {
            inventory=add_checked(inventory,lot.quantity);
            const auto pnl=lot.quantity>0?mul_checked(top.bid-lot.price,static_cast<std::uint64_t>(lot.quantity)):
                mul_checked(lot.price-top.ask,static_cast<std::uint64_t>(-lot.quantity));
            unrealized=add_checked(unrealized,pnl);
        }
        if(inventory!=risk_.position())throw ProtocolError("simulation inventory ledger mismatch");
        stats_.unrealized=unrealized;
    }
    risk_.update_pnl(stats_.realized,stats_.unrealized,stats_.fees,stats_.rebates,stats_.pnl_mark_valid);
    stats_.net=risk_.pnl().net;
    if(stats_.pnl_mark_valid) {
        if(stats_.net<0&&risk_.pnl().high_watermark>INT64_MAX+stats_.net)
            throw ProtocolError("simulation drawdown overflow");
        stats_.max_drawdown=std::max(stats_.max_drawdown,risk_.pnl().high_watermark-stats_.net);
    }
    observe_account_valuation();
}
void Simulator::observe_account_valuation() {
    // Validate accounting even when the quote is halted/stale. The existing
    // risk/legacy-PnL path above intentionally retains its original semantics.
    std::int64_t inventory=0;
    for(const auto& lot:lots_)inventory=add_checked(inventory,lot.quantity);
    if(inventory!=risk_.position())throw ProtocolError("simulation valuation inventory mismatch");
    const bool flat=risk_.position()==0;
    if(flat&&(!lots_.empty()||stats_.cash!=stats_.realized))
        throw ProtocolError("simulation flat ledger does not reconcile");
    if(!flat&&!stats_.pnl_mark_valid) {
        valuation_path_complete_=false;
        return;
    }
    // Flat account equity is known from settled fills even while cancellation
    // acknowledgements are pending. Such a pending account is NOT publishable
    // as a reconciled terminal ledger; json() separately requires no open risk.
    const auto unrealized=flat?0:stats_.unrealized;
    auto net=add_checked(stats_.realized,unrealized);
    net=add_checked(net,-stats_.fees);net=add_checked(net,stats_.rebates);
    valuation_high_watermark_=std::max(valuation_high_watermark_,net);
    if(net<0&&valuation_high_watermark_>INT64_MAX+net)
        throw ProtocolError("simulation valuation drawdown overflow");
    valuation_max_drawdown_=std::max(valuation_max_drawdown_,valuation_high_watermark_-net);
}
void Simulator::submit(Side side,Price price,Qty quantity) {
    if(quantity==0||!side_enabled(side))return;
    // Existing sells, including pending cancels, are reserved in min_position.
    // Reject the entire new request if actual holdings cannot cover every sell.
    // A predicted buy or an unfilled buy reservation never funds a sell.
    if(config_.long_only&&side==Side::Sell&&risk_.min_position()<static_cast<std::int64_t>(quantity)) {
        ++stats_.long_only_rejections;return;
    }
    const auto id=++order_id_;
    const RiskContext context{true,true,true,true,now_,now_};
    const auto verdict=risk_.check_and_reserve({id,side,quantity,price},context);
    if(verdict!=RiskReject::None){++stats_.risk_rejections;return;}
    const auto i=side==Side::Buy?0U:1U;
    WorkingOrder o;o.id=id;o.side=side;o.price=price;o.leaves=quantity;o.submitted_ns=now_;
    o.active_after_ns=deadline(now_,config_.latency_ns);working_[i]=std::move(o);++stats_.orders_submitted;
    hash_value(decision_hash_,sequence_);hash_value(decision_hash_,id);hash_value(decision_hash_,static_cast<std::uint64_t>(price));hash_value(decision_hash_,quantity);
}
bool Simulator::side_enabled(Side side) const noexcept {
    if(!config_.participation_gate_required)return true;
    return gate_seen_&&now_>=config_.trade_start_ns&&now_<config_.trade_end_ns&&now_<gate_expires_ns_&&
        (side==Side::Buy?gate_bid_:gate_ask_);
}
void Simulator::set_participation(std::uint64_t ordinal,bool bid,bool ask,std::uint64_t expires) {
    if(!config_.participation_gate_required||ordinal!=sequence_||ordinal<=last_signal_ordinal_)
        throw ProtocolError("side signal must apply once at its exact current event ordinal");
    if(expires>now_&&expires-now_>config_.max_signal_age_ns)
        throw ProtocolError("side signal validity exceeds configured maximum horizon");
    last_signal_ordinal_=ordinal;++stats_.signals_applied;
    gate_expires_ns_=expires;
    gate_seen_=expires>now_;gate_bid_=gate_seen_&&bid;gate_ask_=gate_seen_&&ask;
    if(!gate_seen_)++stats_.expired_signal_rows;
    hash_value(decision_hash_,ordinal);hash_value(decision_hash_,bid);hash_value(decision_hash_,ask);hash_value(decision_hash_,expires);
}
void Simulator::after_event(const Event& event,const Book& book) {
    mark(event,book);
    if(risk_.killed()||!stats_.pnl_mark_valid||!symbol_||!book.tradable(*symbol_)) {
        if(config_.participation_gate_required){gate_seen_=false;gate_bid_=false;gate_ask_=false;}
        cancel(0);cancel(1);return;
    }
    if(!risk_.arm_after_reconciliation())return;
    // At the exact hard boundary retain a risk-reducing quote. The original
    // pricing helper returns no two-sided quote at the boundary; clamp only its
    // skew input and suppress the increasing side using true risk endpoints.
    const auto skew_inventory=std::clamp(risk_.position(),-config_.hard_inventory_limit+1,config_.hard_inventory_limit-1);
    const auto top=book.top(*symbol_);
    const auto existing_half=std::max(static_cast<long double>(pricing_.min_half_spread),static_cast<long double>(top.ask-top.bid)/2);
    if(static_cast<long double>(quote_cost_floor(pricing_))>existing_half)++stats_.cost_widened_decisions;
    const auto quote=price_quote(top,skew_inventory,pricing_);
    if(!quote){++stats_.no_valid_quote_decisions;cancel(0);cancel(1);return;}
    ++stats_.decisions;
    std::array<Price,2> prices{quote->bid,quote->ask};
    std::array<Qty,2> sizes{quote->bid_size,quote->ask_size};
    // Soft limit suppresses the risk-increasing side; the other side remains
    // eligible subject to the ordinary worst-case reservation and price checks.
    if(risk_.max_position()>=config_.soft_inventory_limit)sizes[0]=0;
    if(risk_.min_position()<=-config_.soft_inventory_limit)sizes[1]=0;
    if(!side_enabled(Side::Buy))sizes[0]=0;
    if(!side_enabled(Side::Sell))sizes[1]=0;
    if(config_.participation_gate_required&&sizes[0]==0&&sizes[1]==0)++stats_.gate_denied_decisions;
    for(std::size_t i=0;i<2;++i) {
        if(working_[i]) {
            const auto& o=*working_[i];
            if(sizes[i]==0||o.price!=prices[i]||now_-o.submitted_ns>=config_.quote_lifetime_ns)cancel(i);
        } else submit(i==0?Side::Buy:Side::Sell,prices[i],sizes[i]);
    }
}
std::size_t Simulator::open_orders() const noexcept {return static_cast<std::size_t>(working_[0].has_value())+static_cast<std::size_t>(working_[1].has_value());}
std::string Simulator::json() const {
    std::ostringstream s;
    s<<std::setprecision(17);
    if(open_orders()!=risk_.open_order_count())throw ProtocolError("simulation open-order valuation mismatch");
    const bool flat=risk_.position()==0&&lots_.empty()&&open_orders()==0&&
        risk_.min_position()==0&&risk_.max_position()==0&&stats_.cash==stats_.realized;
    const bool valuation_valid=stats_.pnl_mark_valid||flat;
    const bool drawdown_complete=valuation_valid&&valuation_path_complete_;
    const auto valuation_unrealized=stats_.pnl_mark_valid?stats_.unrealized:0;
    std::int64_t valuation_net=0;
    if(valuation_valid) {
        valuation_net=add_checked(stats_.realized,valuation_unrealized);
        valuation_net=add_checked(valuation_net,-stats_.fees);valuation_net=add_checked(valuation_net,stats_.rebates);
        if(stats_.pnl_mark_valid&&valuation_net!=stats_.net)
            throw ProtocolError("simulation marked valuation does not reconcile");
    }
    s<<"{\"model_version\":\"displayed_fifo_conservative_v2_cost_aware\",\"currency\":\"USD\",\"price_units_per_dollar\":10000,"
     <<"\"orders_submitted\":"<<stats_.orders_submitted<<",\"orders_activated\":"<<stats_.orders_activated
     <<",\"orders_canceled\":"<<stats_.orders_canceled<<",\"open_orders_at_end\":"<<open_orders()
     <<",\"risk_rejections\":"<<stats_.risk_rejections<<",\"fill_count\":"<<stats_.fill_count
     <<",\"filled_quantity\":"<<stats_.filled_quantity<<",\"queue_consumed\":"<<stats_.queue_consumed
     <<",\"cancel_requests\":"<<stats_.cancel_requests<<",\"fills_while_cancel_pending\":"<<stats_.fills_while_cancel_pending
     <<",\"post_only_rejections\":"<<stats_.post_only_rejections
     <<",\"long_only_rejections\":"<<stats_.long_only_rejections
     <<",\"realized_pnl_units\":"<<stats_.realized<<",\"unrealized_pnl_units\":"<<stats_.unrealized
     <<",\"fees_units\":"<<stats_.fees<<",\"rebates_units\":"<<stats_.rebates
     <<",\"net_pnl_units\":"<<stats_.net<<",\"drawdown_units\":"<<stats_.max_drawdown
     <<",\"cash_before_fees_units\":"<<stats_.cash<<",\"inventory\":"<<risk_.position()
     <<",\"worst_long_inventory\":"<<risk_.max_position()<<",\"worst_short_inventory\":"<<risk_.min_position()
     <<",\"killed\":"<<(risk_.killed()?"true":"false")<<",\"pnl_mark_valid\":"<<(stats_.pnl_mark_valid?"true":"false")
     <<",\"valuation\":{\"schema_version\":1,\"basis\":\""
     <<(stats_.pnl_mark_valid?"fresh_bbo":flat?"reconciled_flat_ledger":"unavailable")
     <<"\",\"quote_mark_valid\":"<<(stats_.pnl_mark_valid?"true":"false")
     <<",\"net_pnl_valid\":"<<(valuation_valid?"true":"false")<<",\"unrealized_pnl_units\":";
    if(valuation_valid)s<<valuation_unrealized;else s<<"null";
    s<<",\"net_pnl_units\":";
    if(valuation_valid)s<<valuation_net;else s<<"null";
    s<<",\"drawdown_complete\":"<<(drawdown_complete?"true":"false")<<",\"max_drawdown_units\":";
    if(drawdown_complete)s<<valuation_max_drawdown_;else s<<"null";
    s<<"},\"execution_metrics\":{\"buy_quantity\":"<<stats_.buy_quantity<<",\"sell_quantity\":"<<stats_.sell_quantity
     <<",\"buy_notional_units\":"<<stats_.buy_notional<<",\"sell_notional_units\":"<<stats_.sell_notional
     <<",\"turnover_units\":"<<stats_.turnover<<",\"peak_abs_inventory_shares\":"<<stats_.peak_abs_inventory
     <<",\"peak_abs_inventory_in_window_shares\":"<<stats_.peak_abs_inventory_in_window
     <<",\"closed_inventory_episodes\":"<<stats_.closed_inventory_episodes<<",\"holding_total_ns\":"<<stats_.holding_total_ns
     <<",\"holding_max_ns\":"<<stats_.holding_max_ns<<",\"mean_closed_holding_ns\":";
    if(stats_.closed_inventory_episodes)s<<static_cast<long double>(stats_.holding_total_ns)/stats_.closed_inventory_episodes;else s<<"null";
    s<<",\"open_holding_ns\":"<<(holding_start_ns_?now_-*holding_start_ns_:0)
     <<",\"observed_window_duration_ns\":"<<stats_.window_observed_ns
     <<",\"full_observed_duration_ns\":"<<(first_event_ns_?now_-*first_event_ns_:0)
     <<",\"inventory_share_seconds\":"<<inventory_share_ns_/1'000'000'000.0L
     <<",\"time_weighted_abs_inventory_shares\":";
    if(stats_.window_observed_ns)s<<inventory_share_ns_/stats_.window_observed_ns;else s<<"null";
    s<<"},\"signal_gate\":{\"enabled\":"<<(config_.participation_gate_required?"true":"false")
     <<",\"default_policy\":\"deny_both_sides\",\"policy_version\":\"ordinal_side_gate_v1\",\"signals_applied\":"<<stats_.signals_applied
     <<",\"expired_signal_rows\":"<<stats_.expired_signal_rows<<",\"gate_denied_decisions\":"<<stats_.gate_denied_decisions
     <<",\"trade_start_ns\":"<<config_.trade_start_ns<<",\"trade_end_ns\":"<<config_.trade_end_ns
     <<",\"max_signal_age_ns\":"<<config_.max_signal_age_ns<<",\"window_end_observed\":"<<(window_end_observed_?"true":"false")
     <<",\"inventory_at_window_end\":";
    if(window_end_observed_)s<<inventory_at_window_end_;else s<<"null";
    s<<",\"fills_after_window_end\":"<<stats_.fills_after_window_end<<",\"quantity_after_window_end\":"<<stats_.quantity_after_window_end
     <<",\"turnover_after_window_end_units\":"<<stats_.turnover_after_window_end<<"}"
     <<",\"pricing_economics\":{\"policy_version\":\""<<quote_economics_policy_version<<"\","
     <<"\"reference\":\"inventory_skewed_reservation_price\",\"fee_per_share_units\":"<<config_.fee_per_share_units
     <<",\"min_fee_per_order_units\":"<<config_.min_fee_per_order_units<<",\"slippage_buffer_units\":"<<config_.slippage_buffer_units
     <<",\"min_net_edge_units\":"<<config_.min_net_edge_units<<",\"cost_half_spread_floor_units\":"<<quote_cost_floor(pricing_)
     <<",\"minimum_commission_allocation_shares\":1,\"rebates_reduce_quote_floor\":false,"
     <<"\"commission_model\":\"max(min_order_fee, cumulative_filled_quantity * fee_per_share)\","
     <<"\"zero_fill_outcome\":"<<(stats_.fill_count==0?"true":"false")
     <<",\"cost_widened_decisions\":"<<stats_.cost_widened_decisions<<",\"no_valid_quote_decisions\":"<<stats_.no_valid_quote_decisions
     <<",\"fee_charged_orders\":"<<order_fees_.size()<<"}"
     <<",\"config\":{\"cost_policy_version\":\""<<quote_economics_policy_version<<"\",\"latency_ns\":"<<config_.latency_ns<<",\"cancel_latency_ns\":"<<config_.cancel_latency_ns
     <<",\"quote_lifetime_ns\":"<<config_.quote_lifetime_ns<<",\"fee_per_share_units\":"<<config_.fee_per_share_units
     <<",\"rebate_per_share_units\":"<<config_.rebate_per_share_units
     <<",\"min_fee_per_order_units\":"<<config_.min_fee_per_order_units<<",\"slippage_buffer_units\":"<<config_.slippage_buffer_units
     <<",\"min_net_edge_units\":"<<config_.min_net_edge_units
     <<",\"max_order_size\":"<<config_.max_order_size<<",\"soft_inventory_limit\":"<<config_.soft_inventory_limit
     <<",\"hard_inventory_limit\":"<<config_.hard_inventory_limit<<",\"orders_per_second\":"<<config_.orders_per_second
     <<",\"tick_size\":"<<config_.tick_size
     <<",\"participation_gate_required\":"<<(config_.participation_gate_required?"true":"false")
     <<",\"long_only\":"<<(config_.long_only?"true":"false")
     <<",\"trade_start_ns\":"<<config_.trade_start_ns<<",\"trade_end_ns\":"<<config_.trade_end_ns
     <<",\"max_signal_age_ns\":"<<config_.max_signal_age_ns
     <<",\"daily_loss_limit_units\":"<<config_.daily_loss_limit_units
     <<",\"daily_drawdown_limit_units\":"<<config_.daily_loss_limit_units<<"},"
     <<"\"assumptions\":[\"Single instrument, price-time displayed FIFO model, no live orders\","
     <<"\"Only later E/C executions at the exact resting price can trigger synthetic fills\","
     <<"\"All visible quantity at activation is ahead; cancellations ahead are not credited\","
     <<"\"Public P/Q and unknown hidden liquidity never generate account fills\","
     <<"\"Strict delayed activation and cancellation; pending cancels retain fill exposure\","
     <<"\"Simulated post-only rejects marketable orders at delayed activation\","
     <<"\"No market impact, hidden priority, broker reject model or calibrated execution probability\","
     <<"\"A broken public trade underlying a modeled fill fails the simulation instead of inventing an account correction\","
     <<"\"FIFO lots; conservative BBO mark; no forced end-of-file liquidation\","
     <<"\"Unconfirmed orders and stale marks are reported explicitly\","
     <<"\"New-order token bucket is modeled; broker aggregate/cancel pacing is not modeled\","
     <<"\"Simulation results do not guarantee profitability or live execution\"]}";
    return s.str();
}
}
