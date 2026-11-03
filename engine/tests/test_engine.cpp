#include "trading/book.hpp"
#include "trading/risk.hpp"
#include "trading/sequencer.hpp"
#include "trading/strategy.hpp"
#include <algorithm>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace trading;
namespace {
void require(bool value,const char* message){if(!value)throw std::runtime_error(message);}
template<class F>void throws(F&& f){bool caught=false;try{f();}catch(const ProtocolError&){caught=true;}require(caught,"expected ProtocolError");}
void put(std::vector<std::byte>& b,std::size_t at,std::uint64_t value,std::size_t width){
    for(std::size_t i=0;i<width;++i){b.at(at+width-i-1)=static_cast<std::byte>(value&255);value>>=8;}
}
std::vector<std::byte> wire(char type,std::size_t len){std::vector<std::byte>b(len);put(b,0,static_cast<unsigned char>(type),1);put(b,1,1,2);return b;}
Event add(RefId id,Side side,Price price,Qty qty=100){Event e;e.type='A';e.locate=1;e.reference=id;e.side=side;e.price=price;e.quantity=qty;e.stock={'T','E','S','T',' ',' ',' ',' '};return e;}
Event event(char type,RefId ref=0,std::uint64_t qty=0){Event e;e.type=type;e.locate=1;e.reference=ref;e.quantity=qty;return e;}
std::vector<std::byte> packet(std::uint64_t first,std::uint16_t count,char session='A'){
    std::vector<std::byte>b(20);for(std::size_t i=0;i<10;++i)b[i]=static_cast<std::byte>(session);put(b,10,first,8);put(b,18,count,2);
    if(count!=65535)for(unsigned i=0;i<count;++i){b.push_back(std::byte{0});b.push_back(std::byte{1});b.push_back(std::byte{42});}return b;
}
RiskLimits limits(){RiskLimits l;l.max_order=100;l.max_position=100;l.price_high=1'000'000;l.max_notional=1'000'000'000;l.daily_loss=1000;l.daily_drawdown=1000;return l;}
RiskContext healthy(){return {true,true,true,true,0,0};}
void arm(RiskEngine& r){r.update_pnl(0,0,0,0,true);require(r.arm_after_reconciliation(),"arm failed");}
}
int main(){
    std::vector<std::pair<std::string,std::function<void()>>> tests={
    {"decoder_add_network_byte_order",[]{
        auto b=wire('A',36);put(b,11,0x0102030405060708ULL,8);put(b,19,'B',1);put(b,20,100,4);put(b,32,1234500,4);put(b,5,123456789,6);
        auto e=decode_itch(b);require(e.reference==0x0102030405060708ULL&&e.quantity==100&&e.price==1234500&&e.timestamp_ns==123456789,"decode fields");
    }},
    {"decoder_truncated_and_invalid",[]{auto b=wire('A',35);throws([&]{decode_itch(b);});b=wire('?',11);throws([&]{decode_itch(b);});b=wire('A',36);put(b,19,'Z',1);throws([&]{decode_itch(b);});}},
    {"decoder_execute_printable",[]{auto b=wire('C',36);put(b,11,8,8);put(b,19,25,4);put(b,23,9,8);put(b,31,'N',1);put(b,32,12345,4);auto e=decode_itch(b);require(!e.printable&&e.match==9&&e.price==12345,"C fields");}},
    {"decoder_every_supported_type",[]{
        const std::vector<std::pair<char,std::size_t>> types={{'S',12},{'R',39},{'H',25},{'Y',20},{'L',26},{'V',35},{'W',12},{'K',28},{'J',35},{'h',21},{'D',19},{'B',19},{'I',50},{'N',20}};
        for(auto [t,n]:types){auto b=wire(t,n);if(t=='S')put(b,11,'O',1);require(decode_itch(b).type==t,"type length");b.push_back(std::byte{0});throws([&]{decode_itch(b);});}
    }},
    {"bounded_parser_random_inputs",[]{
        std::uint64_t seed=0x8a7f1234;
        auto next=[&](){seed^=seed<<13;seed^=seed>>7;seed^=seed<<17;return seed;};
        for(unsigned i=0;i<10000;++i){std::vector<std::byte>b(next()%70);for(auto& byte:b)byte=static_cast<std::byte>(next()&255U);try{(void)decode_itch(b);}catch(const ProtocolError&){}}
    }},
    {"book_add_partial_cancel_delete",[]{Book b;b.apply(add(1,Side::Buy,10000));b.apply(add(2,Side::Buy,10000));b.apply(add(3,Side::Sell,10200));b.apply(event('X',1,40));require(b.top(1).bid_qty==160,"cancel is partial");b.apply(event('D',1));require(b.top(1).bid_qty==100,"delete remainder");b.validate();}},
    {"best_level_empty_advances",[]{Book b;b.apply(add(1,Side::Buy,10000));b.apply(add(2,Side::Buy,9900));b.apply(add(3,Side::Sell,10200));b.apply(event('D',1));require(b.top(1).bid==9900,"best bid advance");b.validate();}},
    {"replace_new_identity_and_absolute_quantity",[]{Book b;b.apply(add(1,Side::Buy,10000));auto e=event('U',1,25);e.new_reference=2;e.price=9900;b.apply(e);b.apply(add(3,Side::Sell,10200));require(b.top(1).bid==9900&&b.top(1).bid_qty==25,"replace absolute");throws([&]{b.apply(event('D',1));});b.validate();}},
    {"book_integrity_fail_closed",[]{Book b;b.apply(add(1,Side::Buy,10000));throws([&]{b.apply(add(1,Side::Buy,10000));});throws([&]{b.apply(event('X',1,101));});throws([&]{b.apply(event('D',2));});b.validate();}},
    {"public_trade_does_not_reduce_book",[]{Book b;b.apply(add(1,Side::Buy,10000));b.apply(add(2,Side::Sell,10200));auto e=event('P',777,20);e.price=10100;e.match=99;b.apply(e);require(b.top(1).bid_qty==100&&b.reported_volume()==20,"public trade mutation");}},
    {"broken_trade_never_restores_order",[]{Book b;b.apply(add(1,Side::Buy,10000));b.apply(add(2,Side::Sell,10200));auto e=event('E',1,30);e.match=99;b.apply(e);require(b.top(1).bid_qty==70&&b.reported_volume()==30,"execute");e=event('B');e.match=99;b.apply(e);require(b.top(1).bid_qty==70&&b.reported_volume()==0,"bust restores volume only");b.validate();}},
    {"nonprintable_execution_still_reduces",[]{Book b;b.apply(add(1,Side::Buy,10000));b.apply(add(2,Side::Sell,10200));auto e=event('C',1,30);e.price=9990;e.match=99;e.printable=false;b.apply(e);require(b.top(1).bid_qty==70&&b.reported_volume()==0,"nonprintable semantics");}},
    {"trading_status_gates_quotes",[]{Book b;b.apply(add(1,Side::Buy,10000));b.apply(add(2,Side::Sell,10200));require(!b.tradable(1),"missing start is not valid");Book c;auto e=event('S');e.status='O';c.apply(e);e.status='Q';c.apply(e);e=event('H');e.status='T';c.apply(e);c.apply(add(1,Side::Buy,10000));c.apply(add(2,Side::Sell,10200));require(c.tradable(1),"continuous");e=event('h');e.status='H';c.apply(e);require(!c.tradable(1),"operational halt");}},
    {"deterministic_checksum_not_hash_iteration",[]{Book a,b;a.apply(add(1,Side::Buy,10000));a.apply(add(2,Side::Sell,10200));b.apply(add(2,Side::Sell,10200));b.apply(add(1,Side::Buy,10000));require(a.checksum()==b.checksum(),"stable checksum");}},
    {"sequence_gap_duplicate_overlap",[]{MoldSequencer s;std::vector<std::uint64_t> seen;auto cb=[&](auto seq,auto){seen.push_back(seq);};require(s.receive(packet(3,1),cb).status==SequenceStatus::Gap&&seen.empty(),"gap gate");s.receive(packet(1,2),cb);require(s.receive(packet(1,2),cb).status==SequenceStatus::Duplicate,"dedup");s.receive(packet(2,3),cb);require(seen==std::vector<std::uint64_t>({1,2,3,4})&&s.expected()==5,"overlap");}},
    {"sequence_validation_before_commit",[]{MoldSequencer s;int n=0;auto cb=[&](auto,auto){++n;};auto p=packet(1,2);p.pop_back();throws([&]{s.receive(p,cb);});require(n==0&&s.expected()==1,"no partial malformed commit");s.receive(packet(1,1),cb);throws([&]{s.receive(packet(2,1,'B'),cb);});}},
    {"sequence_heartbeat_end",[]{MoldSequencer s;auto cb=[](auto,auto){};require(s.receive(packet(1,0),cb).status==SequenceStatus::Heartbeat,"heartbeat");require(s.receive(packet(1,65535),cb).status==SequenceStatus::SessionEnd,"end");throws([&]{s.receive(packet(1,1),cb);});}},
    {"risk_requires_arm_and_health",[]{RiskEngine r(limits());auto c=healthy();require(r.check_and_reserve({1,Side::Buy,10,10000},c)==RiskReject::Disabled,"default disabled");arm(r);c.market_valid=false;require(r.check_and_reserve({1,Side::Buy,10,10000},c)==RiskReject::Unhealthy,"invalid market");}},
    {"risk_worst_direction_not_net",[]{RiskEngine r(limits());arm(r);require(r.check_and_reserve({1,Side::Buy,100,10000},healthy())==RiskReject::None,"buy reserve");require(r.check_and_reserve({2,Side::Sell,100,10000},healthy())==RiskReject::None,"sell reserve");require(r.check_and_reserve({3,Side::Buy,1,10000},healthy())==RiskReject::Inventory,"opposing side not netted");}},
    {"cancel_unknown_keep_reservation",[]{RiskEngine r(limits());arm(r);r.check_and_reserve({1,Side::Buy,100,10000},healthy());r.request_cancel(1);require(r.max_position()==100,"pending cancel retains");r.mark_unknown(1);require(r.max_position()==100,"unknown retains");r.confirm_terminal(1,LocalOrderState::Canceled);require(r.max_position()==0,"confirmed cancel releases");}},
    {"partial_fill_transfer_and_dedup",[]{RiskEngine r(limits());arm(r);r.check_and_reserve({1,Side::Buy,100,10000},healthy());require(r.fill(99,1,30,10000),"first fill");require(r.position()==30&&r.max_position()==100,"risk transfer");require(!r.fill(99,1,30,10000)&&r.position()==30,"dedup");throws([&]{r.fill(99,1,31,10000);});r.confirm_terminal(1,LocalOrderState::Canceled);require(r.max_position()==30,"cancel rest");}},
    {"late_fill_after_cancel_updates_and_kills",[]{RiskEngine r(limits());arm(r);r.check_and_reserve({1,Side::Buy,100,10000},healthy());r.confirm_terminal(1,LocalOrderState::Canceled);r.check_and_reserve({2,Side::Buy,100,10000},healthy());r.fill(90,1,100,10000);require(r.position()==100&&r.killed(),"late fill preserved");}},
    {"late_fill_rechecks_tighter_notional_both_directions",[]{
        for(const auto side:{Side::Buy,Side::Sell}){
            auto l=limits();l.max_order=1;l.max_position=5;l.price_high=10000;l.max_notional=10000;
            RiskEngine r(l);arm(r);
            require(r.check_and_reserve({1,side,1,10000},healthy())==RiskReject::None,"old order admitted");
            require(r.confirm_terminal(1,LocalOrderState::Canceled),"old cancel confirmed");
            require(r.check_and_reserve({2,side,1,10000},healthy())==RiskReject::None,"new reservation admitted");
            require(r.fill(90,1,1,10000),"late fill recorded");
            require(r.killed()&&!r.armed(),"notional-only breach latches kill");
            require(r.position()==(side==Side::Buy?1:-1),"late position retained");
            require(r.open_order_count()==1&&r.orders().at(2).reserved&&r.orders().at(2).leaves==1,"new in-flight exposure retained");
            require(side==Side::Buy?r.max_position()==2:r.min_position()==-2,"worst direction includes both risks");
            require(r.check_and_reserve({3,side,1,10000},healthy())==RiskReject::Disabled,"further new orders blocked");
        }
    }},
    {"loss_and_drawdown_are_latched",[]{RiskEngine r(limits());arm(r);r.update_pnl(0,-900,100,0,true);require(r.killed()&&r.pnl().net==-1000,"fee net loss");r.update_pnl(10000,0,0,0,true);require(!r.arm_after_reconciliation(),"strategy cannot reset kill");RiskEngine d(limits());arm(d);d.update_pnl(2000,0,0,0,true);d.update_pnl(1000,0,0,0,true);require(d.killed(),"drawdown");}},
    {"stale_and_duplicate_and_numeric_limits",[]{RiskEngine r(limits());arm(r);auto c=healthy();c.now_ns=2'000'000'000;require(r.check_and_reserve({1,Side::Buy,10,10000},c)==RiskReject::StaleMarket,"stale");r.check_and_reserve({1,Side::Buy,10,10000},healthy());require(r.check_and_reserve({1,Side::Buy,10,10000},healthy())==RiskReject::Duplicate,"duplicate");throws([&]{r.update_pnl(INT64_MAX,1,0,0,true);});}},
    {"token_fraction_refill_and_clock_regression",[]{TokenBucket b(2,2,0);require(b.take()&&b.take()&&!b.take(),"burst");b.refill(250'000'000);require(!b.take(),"fraction");b.refill(500'000'000);require(b.take(),"accumulated fraction");require(!b.refill(1),"clock regression");b.refill(UINT64_MAX);require(b.take(2),"saturating refill");throws([]{TokenBucket too_big(UINT64_MAX,1,0);});}},
    {"inventory_skew_and_tick",[]{Top top{10000,10400,100,100,true};PricingConfig c;auto flat=price_quote(top,0,c),longq=price_quote(top,500,c),shortq=price_quote(top,-500,c);require(flat&&longq&&shortq,"quote exists");require(longq->bid<flat->bid&&longq->ask<flat->ask,"long skew");require(shortq->bid>flat->bid&&shortq->ask>flat->ask,"short skew");require(longq->bid%100==0&&longq->ask%100==0,"round tick");require(!price_quote(top,1000,c),"hard limit");auto extreme=price_quote(top,999,c);require(extreme&&extreme->ask>top.bid,"research quote remains passive");}},
    {"quote_floor_uses_one_share_minimum_and_inventory_reference",[]{
        Top top{10000,10400,100,100,true};PricingConfig c;
        c.fee_per_share_units=30;c.min_fee_per_order_units=500;c.slippage_buffer_units=70;c.min_net_edge_units=100;
        require(quote_cost_floor(c)==700,"sum is conservative minimum-one-share floor");
        const auto flat=price_quote(top,0,c),longq=price_quote(top,500,c);
        require(flat&&flat->bid==9500&&flat->ask==10900,"both sides respect cost floor around 10200 reservation");
        require(longq&&9950-longq->bid>=700&&longq->ask-9950>=700,"skewed reservation bounds");
        require(longq->ask-10200<700,"floor must not be misdescribed as edge against unskewed fair");
    }},
    {"quote_economics_bounds_and_no_positive_price",[]{
        PricingConfig c;c.fee_per_share_units=-1;throws([&]{quote_cost_floor(c);});
        c.fee_per_share_units=INT64_MAX;throws([&]{quote_cost_floor(c);});
        c.fee_per_share_units=c.min_fee_per_order_units=c.slippage_buffer_units=c.min_net_edge_units=1'000'000'000;
        require(quote_cost_floor(c)==4'000'000'000,"bounded sum no overflow");
        require(!price_quote(Top{10000,10400,100,100,true},0,c),"costs can eliminate valid positive bid");
    }},
    {"default_commission_floor_below_tick_preserves_existing_quote",[]{
        const Top top{10000,10400,100,100,true};PricingConfig c;const auto old=price_quote(top,0,c);
        c.fee_per_share_units=30;const auto current=price_quote(top,0,c);
        require(old&&current&&old->bid==current->bid&&old->ask==current->ask,"30 unit fee does not override 200 unit existing half-spread");
    }}
    };
    int failures=0;for(const auto& [name,test]:tests){try{test();std::cout<<"PASS "<<name<<'\n';}catch(const std::exception& e){++failures;std::cerr<<"FAIL "<<name<<": "<<e.what()<<'\n';}}
    std::cout<<tests.size()<<" tests, "<<failures<<" failures\n";return failures?1:0;
}
