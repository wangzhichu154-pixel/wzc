#include "trading/vector_core.h"
#include <cstring>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
void expect(bool value,const char* message){if(!value)throw std::runtime_error(message);}
void put(std::vector<uint8_t>& b,size_t at,uint64_t value,size_t width){for(size_t i=0;i<width;++i){b.at(at+width-i-1)=static_cast<uint8_t>(value&255);value>>=8;}}
std::vector<uint8_t> message(char type,size_t length,uint16_t locate=1){std::vector<uint8_t>b(length);b[0]=static_cast<uint8_t>(type);put(b,1,locate,2);return b;}
std::vector<uint8_t> system(char code){auto b=message('S',12,0);b[11]=static_cast<uint8_t>(code);return b;}
std::vector<uint8_t> status(char code){auto b=message('H',25);std::memcpy(b.data()+11,"TEST    ",8);b[19]=static_cast<uint8_t>(code);return b;}
std::vector<uint8_t> add(uint64_t ref,char side,uint32_t price){auto b=message('A',36);put(b,11,ref,8);b[19]=static_cast<uint8_t>(side);put(b,20,100,4);std::memcpy(b.data()+24,"TEST    ",8);put(b,32,price,4);return b;}
vc_config config(){vc_config c{};c.abi_version=VC_ABI_VERSION;c.struct_size=sizeof(c);c.symbol_locate=1;c.max_order_size=100;c.hard_inventory_limit=1000;c.max_notional_units=1'000'000'000;c.max_open_orders=10;c.daily_loss_limit_units=1000;c.daily_drawdown_limit_units=1000;c.max_market_age_ns=1000;c.orders_per_second=100;c.price_low=1;c.price_high=1'000'000;c.tick_size=100;c.max_book_orders=1000;c.max_trade_records=1000;std::memcpy(c.symbol,"TEST",4);return c;}
struct Core {
    vc_handle* h{};
    explicit Core(bool configure_economics=true){const auto c=config();expect(vc_create(&c,&h)==VC_OK&&h,"create");if(configure_economics)expect(vc_set_quote_economics(h,0,0,0,0)==VC_OK,"explicit quote economics");}
    ~Core(){vc_destroy(h);}
    void feed(const std::vector<uint8_t>& b,uint64_t now){expect(vc_process_itch(h,b.data(),static_cast<uint32_t>(b.size()),now)==VC_OK,"process ITCH");}
    void initialize(bool live=true){
        feed(system('O'),100);feed(system('Q'),101);feed(status('T'),102);feed(add(1,'B',10000),103);feed(add(2,'S',10200),104);
        vc_health health{1,1,1,1,static_cast<uint8_t>(live),{0,0,0}};expect(vc_set_health(h,&health)==VC_OK,"health");
        expect(vc_update_pnl(h,0,0,0,0,1)==VC_OK,"initial reconciled PnL");
    }
    void arm(){expect(vc_arm_flat(h,1,1,0,0,110)==VC_OK,"flat arm");}
    vc_snapshot snapshot(){vc_snapshot s{};expect(vc_get_snapshot(h,&s)==VC_OK,"snapshot");return s;}
    vc_permit reserve(uint64_t id=10,uint64_t now=120,uint64_t expires=500){vc_permit p{};expect(vc_check_and_reserve(h,id,0,10,10000,now,expires,&p)==VC_OK,"reserve");return p;}
};
}
int main(){
    std::vector<std::pair<std::string,std::function<void()>>> cases={
    {"abi_version_and_null_validation",[]{vc_handle* h=nullptr;auto c=config();c.abi_version=999;expect(vc_create(&c,&h)==VC_INVALID_ARGUMENT&&!h,"version fail");expect(vc_create(nullptr,&h)==VC_INVALID_ARGUMENT,"null config");expect(vc_destroy(nullptr)==VC_OK,"null destroy");}},
    {"explicit_quote_economics_required_before_arm",[]{Core c(false);c.initialize();vc_quote q{};expect(vc_get_quote(c.h,110,&q)==VC_OK&&q.market_fresh&&!q.quote_available,"market available, economic intent gated");expect(vc_arm_flat(c.h,1,1,0,0,110)==VC_NOT_READY,"unconfigured economics blocks arm");expect(vc_set_quote_economics(c.h,30,0,0,0)==VC_OK,"explicit economics");c.arm();}},
    {"quote_economics_atomic_validation_and_pricing",[]{Core c;c.initialize();expect(vc_set_quote_economics(c.h,30,500,70,100)==VC_OK,"set floor 700");vc_quote before{},after{};expect(vc_get_quote(c.h,110,&before)==VC_OK&&before.bid_price==9400&&before.ask_price==10800,"cost-aware quote");expect(vc_set_quote_economics(c.h,0,-1,0,0)==VC_INVALID_ARGUMENT,"negative rejected");expect(vc_set_quote_economics(c.h,INT64_MAX,0,0,0)==VC_INVALID_ARGUMENT,"overflow input rejected");expect(vc_get_quote(c.h,111,&after)==VC_OK&&before.bid_price==after.bid_price&&before.ask_price==after.ask_price&&!c.snapshot().killed,"invalid config leaves prior state intact");}},
    {"quote_economics_frozen_even_after_kill_and_empty_book",[]{Core c;c.initialize();c.arm();expect(vc_set_quote_economics(c.h,30,50,0,0)==VC_NOT_READY,"armed reject");expect(vc_kill(c.h,VC_FAULT_MANUAL)==VC_OK,"kill");expect(!c.snapshot().armed&&c.snapshot().open_orders==0,"disarmed and no reserved orders");expect(vc_set_quote_economics(c.h,0,0,0,0)==VC_NOT_READY,"ever-armed immutable after kill");}},
    {"historical_source_cannot_arm",[]{Core c;c.initialize(false);expect(vc_arm_flat(c.h,1,1,0,0,110)==VC_NOT_READY,"recorded source rejected");expect(!c.snapshot().armed,"unarmed");}},
    {"existing_inventory_or_open_orders_cannot_bootstrap",[]{Core c;c.initialize();expect(vc_arm_flat(c.h,1,1,0,1,110)==VC_NOT_READY,"inventory refused");expect(vc_arm_flat(c.h,1,1,1,0,110)==VC_NOT_READY,"open orders refused");expect(vc_arm_flat(c.h,0,1,0,0,110)==VC_NOT_READY,"unverified paper refused");expect(vc_arm_flat(c.h,1,0,0,0,110)==VC_NOT_READY,"missing end markers refused");c.arm();}},
    {"same_core_quote_and_one_shot_permit",[]{Core c;c.initialize();c.arm();vc_quote q{};expect(vc_get_quote(c.h,120,&q)==VC_OK&&q.quote_available&&q.market_fresh&&q.bid_price==10000&&q.ask_price==10200,"quote");auto p=c.reserve();auto forged=p;forged.deadline_ns++;expect(vc_send_boundary(c.h,&forged,125)==VC_INVALID_ARGUMENT,"cannot extend deadline");expect(vc_send_boundary(c.h,&p,130)==VC_OK,"send boundary");expect(vc_send_boundary(c.h,&p,131)==VC_DUPLICATE,"one dispatch");}},
    {"partial_fill_reservation_and_pnl_barrier",[]{Core c;c.initialize();c.arm();auto p=c.reserve();expect(vc_send_boundary(c.h,&p,130)==VC_OK,"send");expect(vc_ack(c.h,10)==VC_OK,"ack");expect(vc_fill(c.h,99,10,5,10000)==VC_OK,"fill");auto s=c.snapshot();expect(s.position==5&&s.buy_reserved==5&&!s.pnl_mark_valid,"partial transfer");vc_permit next{};expect(vc_check_and_reserve(c.h,11,0,1,10000,140,500,&next)==VC_NOT_READY,"PnL must update before next order");expect(vc_update_pnl(c.h,0,0,150,0,1)==VC_OK,"fees");expect(vc_cancel_request(c.h,10)==VC_OK&&c.snapshot().buy_reserved==5,"pending cancel retains");expect(vc_cancel_confirm(c.h,10)==VC_OK&&c.snapshot().buy_reserved==0,"confirmed cancel releases");expect(vc_fill(c.h,100,10,2,10000)==VC_OK&&c.snapshot().position==7,"late fill retained");expect(vc_update_pnl(c.h,0,0,210,0,1)==VC_OK,"late fees");expect(vc_fill(c.h,100,10,2,10000)==VC_DUPLICATE&&c.snapshot().pnl_mark_valid,"duplicate idempotent");}},
    {"expired_not_sent_releases_only_on_confirmation",[]{Core c;c.initialize();c.arm();auto p=c.reserve();expect(vc_send_boundary(c.h,&p,501)==VC_NOT_READY,"expired");expect(c.snapshot().buy_reserved==10,"failed boundary retains until owner confirms");expect(vc_definitely_not_sent(c.h,10)==VC_OK&&c.snapshot().buy_reserved==0,"known non-send releases");}},
    {"unknown_keeps_exposure_and_latches",[]{Core c;c.initialize();c.arm();auto p=c.reserve();expect(vc_send_boundary(c.h,&p,130)==VC_OK,"send");expect(vc_outcome_unknown(c.h,10)==VC_RECONCILIATION_REQUIRED,"unknown");const auto s=c.snapshot();expect(s.killed&&s.reconcile_required&&s.buy_reserved==10,"unknown retained");expect(vc_arm_flat(c.h,1,1,0,0,140)==VC_RECONCILIATION_REQUIRED,"no reset");expect(vc_cancel_request(c.h,10)==VC_OK,"cancel allowed after kill");}},
    {"disconnect_revokes_pending_permit",[]{Core c;c.initialize();c.arm();auto p=c.reserve();vc_health health{0,1,1,1,1,{0,0,0}};expect(vc_set_health(c.h,&health)==VC_RECONCILIATION_REQUIRED,"disconnect");expect(vc_send_boundary(c.h,&p,130)==VC_NOT_READY,"revoked");expect(c.snapshot().reason==VC_FAULT_BROKER,"reason");}},
    {"execution_correction_is_reconciliation_fault",[]{Core c;c.initialize();c.arm();expect(vc_execution_correction(c.h)==VC_RECONCILIATION_REQUIRED,"correction");expect(c.snapshot().reason==VC_FAULT_EXECUTION_CORRECTION,"reason");}},
    {"daily_loss_latches",[]{Core c;c.initialize();c.arm();expect(vc_update_pnl(c.h,0,-900,100,0,1)==VC_RECONCILIATION_REQUIRED,"loss");expect(c.snapshot().killed&&c.snapshot().net_pnl_units==-1000,"net PnL");}},
    {"malformed_feed_exception_does_not_cross_abi",[]{Core c;c.initialize();auto b=message('A',10);expect(vc_process_itch(c.h,b.data(),static_cast<uint32_t>(b.size()),120)==VC_RECONCILIATION_REQUIRED,"malformed");expect(c.snapshot().killed,"protocol latches");char error[256]{};expect(vc_last_error(c.h,error,sizeof(error))==VC_OK&&std::string(error).find("length")!=std::string::npos,"error text");}},
    {"status_or_metadata_does_not_refresh_bbo",[]{Core c;c.initialize();c.arm();auto y=message('Y',20);std::memcpy(y.data()+11,"TEST    ",8);y[19]='0';c.feed(y,1500);vc_quote q{};expect(vc_get_quote(c.h,1501,&q)==VC_OK&&!q.market_fresh&&!q.quote_available&&q.last_valid_bbo_ns==104,"stale mark");}},
    {"halt_latches_working_market_fault",[]{Core c;c.initialize();c.arm();auto p=c.reserve();(void)p;auto halt=status('H');expect(vc_process_itch(c.h,halt.data(),static_cast<uint32_t>(halt.size()),130)==VC_RECONCILIATION_REQUIRED,"halt");const auto s=c.snapshot();expect(s.killed&&s.buy_reserved==10&&!s.pnl_mark_valid,"halt retains risk, invalidates mark");}},
    {"rx_queue_delay_is_not_clock_regression",[]{Core c;c.initialize();vc_quote q{};expect(vc_get_quote(c.h,200,&q)==VC_OK,"clock advance");c.feed(add(3,'B',9900),150);expect(!c.snapshot().killed,"queued rx timestamp allowed");auto e=add(4,'B',9800);expect(vc_process_itch(c.h,e.data(),static_cast<uint32_t>(e.size()),149)==VC_RECONCILIATION_REQUIRED,"actual rx regression fails");}},
    {"single_writer_owner_enforced",[]{Core c;int result=0;std::thread other([&]{vc_snapshot s{};result=vc_get_snapshot(c.h,&s);});other.join();expect(result==VC_INVALID_ARGUMENT,"wrong owner denied");expect(!c.snapshot().killed,"owner still valid");}},
    {"passive_tick_guards",[]{Core c;c.initialize();c.arm();vc_permit p{};expect(vc_check_and_reserve(c.h,10,0,10,10200,120,500,&p)==VC_RISK_REJECTED,"no aggressive order");expect(vc_check_and_reserve(c.h,10,0,10,10001,120,500,&p)==VC_INVALID_ARGUMENT,"tick");}}
    };
    int failures=0;for(const auto& [name,test]:cases){try{test();std::cout<<"PASS "<<name<<'\n';}catch(const std::exception& e){++failures;std::cerr<<"FAIL "<<name<<": "<<e.what()<<'\n';}}
    std::cout<<cases.size()<<" C ABI tests, "<<failures<<" failures\n";return failures?1:0;
}
