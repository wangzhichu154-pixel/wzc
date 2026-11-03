#include "trading/book.hpp"
#include "trading/risk.hpp"
#include "trading/strategy.hpp"
#include "trading/simulation.hpp"
#ifdef TRADING_ML_CLI
#include "trading/ml_io.hpp"
#endif
#include <array>
#include <charconv>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace {
struct Options {
    std::filesystem::path input;
    std::uint64_t max_bytes{1'073'741'824};
    std::uint64_t max_messages{20'000'000};
    std::uint64_t max_orders{2'000'000},max_trades{2'000'000};
    std::uint64_t max_order_size{100},soft_inventory{500},hard_inventory{1000};
    std::uint64_t daily_loss_cents{100000},orders_per_second{5},tick{100};
    bool simulate{false};
    std::uint64_t latency_ns{1'000'000},cancel_latency_ns{1'000'000},window_ns{1'000'000'000};
    std::uint64_t fee_units{30},rebate_units{0};
    std::uint64_t min_fee_units{0},slippage_units{0},net_edge_units{0};
#ifdef TRADING_ML_CLI
    std::filesystem::path export_market,signal_file;
    bool long_only{false};
    std::uint64_t symbol_locate{0},trade_start_ns{0},trade_end_ns{86'400'000'000'000ULL},max_signal_age_ns{1'000'000'000};
#endif
};
std::uint64_t number(const std::string& value,bool allow_zero=false) {
    std::uint64_t out=0;
    const auto result=std::from_chars(value.data(),value.data()+value.size(),out);
    if(result.ec!=std::errc{}||result.ptr!=value.data()+value.size()||(!allow_zero&&out==0))
        throw trading::ProtocolError("expected bounded nonnegative integer option");
    return out;
}
void help() {
#ifdef TRADING_ML_CLI
    std::cout<<"ML research interface: trading-ml-replay --input CAPTURE --symbol-locate N\n"
        "  --export-market NEW.csv\n"
        "  --simulate --signal-file SIGNALS.csv [--trade-start-ns N --trade-end-ns N --max-signal-age-ns N] [--long-only]\n"
        "Signal header: ordinal,allow_bid,allow_ask,expires_ns; initial/expired gate denies both sides.\n"
        "Capture/model/signal SHA256 binding MUST be checked by the controlled Python caller.\n";
#endif
    std::cout<<"Usage: trading-replay --input CAPTURE.itch [options]\n"
        "Input: uncompressed NASDAQ ITCH 5.0, 2-byte big-endian message length framing.\n"
        "No PCAP, no gzip and no broker connection. Default mode has no account fills.\n"
        "Options: --max-order-size N --soft-inventory-limit N --hard-inventory-limit N\n"
        "         --daily-loss-limit-cents N --orders-per-second N --tick-size N\n"
        "         --max-bytes N --max-messages N --max-orders N --max-trades N\n"
        "         --simulate [--latency-ns N --cancel-latency-ns N --simulation-window-ns N]\n"
        "         --fee-per-share-units N --rebate-per-share-units N\n"
        "         --min-fee-per-order-units N --slippage-buffer-units N --min-net-edge-units N\n"
        "Simulation is single-instrument, counterfactual displayed FIFO; never a live broker.\n"
        "Price/tick units: 1/10000 USD. Output: one JSON document on stdout.\n";
}
Options parse(int argc,char** argv) {
    Options o;
    for(int i=1;i<argc;++i) {
        const std::string key=argv[i];
        if(key=="--simulate"){o.simulate=true;continue;}
#ifdef TRADING_ML_CLI
        if(key=="--long-only"){o.long_only=true;continue;}
#endif
        if(i+1>=argc)throw trading::ProtocolError("missing argument value");
        const std::string value=argv[++i];
        if(key=="--input"){if(!o.input.empty())throw trading::ProtocolError("duplicate input");o.input=value;continue;}
#ifdef TRADING_ML_CLI
        if(key=="--export-market"){if(!o.export_market.empty())throw trading::ProtocolError("duplicate market export");o.export_market=value;continue;}
        if(key=="--signal-file"){if(!o.signal_file.empty())throw trading::ProtocolError("duplicate signal file");o.signal_file=value;continue;}
        if(key=="--symbol-locate"){o.symbol_locate=number(value);continue;}
        if(key=="--trade-start-ns"){o.trade_start_ns=number(value,true);continue;}
        if(key=="--trade-end-ns"){o.trade_end_ns=number(value);continue;}
        if(key=="--max-signal-age-ns"){o.max_signal_age_ns=number(value);continue;}
#endif
        const auto n=number(value,key=="--fee-per-share-units"||key=="--rebate-per-share-units"||key=="--latency-ns"||key=="--cancel-latency-ns"||
            key=="--min-fee-per-order-units"||key=="--slippage-buffer-units"||key=="--min-net-edge-units");
        if(key=="--max-bytes")o.max_bytes=n;
        else if(key=="--max-messages")o.max_messages=n;
        else if(key=="--max-orders")o.max_orders=n;
        else if(key=="--max-trades")o.max_trades=n;
        else if(key=="--max-order-size")o.max_order_size=n;
        else if(key=="--soft-inventory-limit")o.soft_inventory=n;
        else if(key=="--hard-inventory-limit")o.hard_inventory=n;
        else if(key=="--daily-loss-limit-cents")o.daily_loss_cents=n;
        else if(key=="--orders-per-second")o.orders_per_second=n;
        else if(key=="--tick-size")o.tick=n;
        else if(key=="--latency-ns")o.latency_ns=n;
        else if(key=="--cancel-latency-ns")o.cancel_latency_ns=n;
        else if(key=="--simulation-window-ns")o.window_ns=n;
        else if(key=="--fee-per-share-units")o.fee_units=n;
        else if(key=="--rebate-per-share-units")o.rebate_units=n;
        else if(key=="--min-fee-per-order-units")o.min_fee_units=n;
        else if(key=="--slippage-buffer-units")o.slippage_units=n;
        else if(key=="--min-net-edge-units")o.net_edge_units=n;
        else throw trading::ProtocolError("unknown command-line option");
    }
    if(o.input.empty())throw trading::ProtocolError("--input is required");
#ifdef TRADING_ML_CLI
    if(o.symbol_locate==0||o.symbol_locate>65535||(o.export_market.empty()&&o.signal_file.empty()))
        throw trading::ProtocolError("ML replay requires --symbol-locate and an export or signal file");
    if(!o.signal_file.empty()&&!o.simulate)throw trading::ProtocolError("--signal-file requires --simulate");
    if(o.simulate&&o.signal_file.empty())throw trading::ProtocolError("ML simulation requires an explicit --signal-file, including for a baseline");
    if(o.long_only&&!o.simulate)throw trading::ProtocolError("--long-only requires --simulate");
    if(o.trade_start_ns>=o.trade_end_ns||o.trade_end_ns>86'400'000'000'000ULL||o.max_signal_age_ns>3'600'000'000'000ULL)
        throw trading::ProtocolError("invalid ML trading window or signal horizon");
#endif
    if(o.max_order_size>1'000'000||o.hard_inventory>1'000'000'000||o.soft_inventory>=o.hard_inventory||
       o.max_order_size>o.hard_inventory||o.daily_loss_cents>1'000'000'000'000ULL||
       o.orders_per_second>1'000'000||o.tick>1'000'000'000||
       o.max_orders>10'000'000||o.max_trades>10'000'000||o.max_messages>100'000'000||
       o.max_bytes>8'589'934'592ULL||o.latency_ns>60'000'000'000ULL||o.cancel_latency_ns>60'000'000'000ULL||
       o.window_ns>3'600'000'000'000ULL||o.fee_units>1'000'000'000||o.rebate_units>1'000'000||
       o.min_fee_units>1'000'000'000||o.slippage_units>1'000'000'000||o.net_edge_units>1'000'000'000)
        throw trading::ProtocolError("option exceeds supported bounds or invalid inventory thresholds");
    return o;
}
std::string hex(std::uint64_t value) {
    std::ostringstream s;s<<std::hex<<std::setfill('0')<<std::setw(16)<<value;return s.str();
}
void mix(std::uint64_t& hash,std::uint64_t value) {
    for(int i=0;i<8;++i){hash^=value&255U;hash*=1099511628211ULL;value>>=8;}
}
}

int main(int argc,char** argv) {
    if(argc==2&&std::string(argv[1])=="--help"){help();return 0;}
    std::uint64_t message_count=0;
    try {
        const auto options=parse(argc,argv);
        std::error_code ec;
        if(!std::filesystem::is_regular_file(options.input,ec)||ec)
            throw trading::ProtocolError("input must be an accessible regular file");
        const auto size=std::filesystem::file_size(options.input,ec);
        if(ec||size==0||size>options.max_bytes)throw trading::ProtocolError("input empty or exceeds byte limit");
        std::ifstream input(options.input,std::ios::binary);
        if(!input)throw trading::ProtocolError("cannot open input file");
        trading::Book book(static_cast<std::size_t>(options.max_orders),static_cast<std::size_t>(options.max_trades));
#ifdef TRADING_ML_CLI
        std::unique_ptr<trading::MarketExporter> exporter;
        std::unique_ptr<trading::SignalReader> signals;
        if(!options.export_market.empty())exporter=std::make_unique<trading::MarketExporter>(options.export_market);
        if(!options.signal_file.empty())signals=std::make_unique<trading::SignalReader>(options.signal_file);
#endif
        trading::PricingConfig pricing;
        pricing.tick=static_cast<trading::Price>(options.tick);
        pricing.min_half_spread=pricing.tick;
        pricing.quote_size=static_cast<trading::Qty>(options.max_order_size);
        pricing.hard_inventory=static_cast<std::int64_t>(options.hard_inventory);
        pricing.fee_per_share_units=static_cast<std::int64_t>(options.fee_units);
        pricing.min_fee_per_order_units=static_cast<std::int64_t>(options.min_fee_units);
        pricing.slippage_buffer_units=static_cast<std::int64_t>(options.slippage_units);
        pricing.min_net_edge_units=static_cast<std::int64_t>(options.net_edge_units);
        (void)trading::quote_cost_floor(pricing);
        std::unique_ptr<trading::Simulator> simulator;
        if(options.simulate) {
            trading::SimulationConfig c;
            c.max_order_size=static_cast<trading::Qty>(options.max_order_size);
            c.soft_inventory_limit=static_cast<std::int64_t>(options.soft_inventory);
            c.hard_inventory_limit=static_cast<std::int64_t>(options.hard_inventory);
            c.daily_loss_limit_units=static_cast<std::int64_t>(options.daily_loss_cents*100);
            c.orders_per_second=options.orders_per_second;c.tick_size=static_cast<trading::Price>(options.tick);
            c.latency_ns=options.latency_ns;c.cancel_latency_ns=options.cancel_latency_ns;c.quote_lifetime_ns=options.window_ns;
            c.fee_per_share_units=static_cast<std::int64_t>(options.fee_units);c.rebate_per_share_units=static_cast<std::int64_t>(options.rebate_units);
            c.min_fee_per_order_units=static_cast<std::int64_t>(options.min_fee_units);
            c.slippage_buffer_units=static_cast<std::int64_t>(options.slippage_units);
            c.min_net_edge_units=static_cast<std::int64_t>(options.net_edge_units);
#ifdef TRADING_ML_CLI
            c.participation_gate_required=signals!=nullptr;
            c.long_only=options.long_only;
            c.trade_start_ns=options.trade_start_ns;c.trade_end_ns=options.trade_end_ns;c.max_signal_age_ns=options.max_signal_age_ns;
#endif
            simulator=std::make_unique<trading::Simulator>(c);
        }
        trading::TokenBucket intents(options.orders_per_second,options.orders_per_second,0);
        std::uint64_t decisions=0,accepted=0,throttled=0,bytes_read=0;
        std::uint64_t decision_hash=14695981039346656037ULL,logical_time=0;
        const auto started=std::chrono::steady_clock::now();
        while(true) {
            std::array<char,2> length{};
            input.read(length.data(),2);
            if(input.gcount()==0&&input.eof())break;
            if(input.gcount()!=2)throw trading::ProtocolError("truncated length prefix");
            const auto n=(static_cast<unsigned>(static_cast<unsigned char>(length[0]))<<8)|
                         static_cast<unsigned>(static_cast<unsigned char>(length[1]));
            if(n<11||n>50)throw trading::ProtocolError("unsupported ITCH frame length");
            if(message_count>=options.max_messages)throw trading::ProtocolError("message limit exceeded");
            if(bytes_read>options.max_bytes||options.max_bytes-bytes_read<2||n>options.max_bytes-bytes_read-2)
                throw trading::ProtocolError("byte limit exceeded during read");
            std::array<std::byte,50> payload{};
            input.read(reinterpret_cast<char*>(payload.data()),n);
            if(input.gcount()!=static_cast<std::streamsize>(n))throw trading::ProtocolError("truncated ITCH message");
            bytes_read+=2+n;
            const auto e=trading::decode_itch(std::span<const std::byte>(payload.data(),n));
#ifdef TRADING_ML_CLI
            if(e.locate!=0&&e.locate!=options.symbol_locate)
                throw trading::ProtocolError("ML replay requires a single configured stock locate; filter the capture with correct order relationships first");
#endif
            // Exchange timestamps do not sequence a feed and may regress. A
            // deterministic monotonic clock is used only for rate accounting.
            logical_time=std::max(logical_time,e.timestamp_ns);
            if(simulator)simulator->before_event(e,book,logical_time,message_count+1);
            book.apply(e);++message_count;
#ifdef TRADING_ML_CLI
            if(exporter)exporter->observe(e,book,static_cast<std::uint16_t>(options.symbol_locate),message_count,logical_time);
            if(signals) {
                const auto signal=signals->at(message_count);
                if(signal)simulator->set_participation(signal->ordinal,signal->allow_bid,signal->allow_ask,signal->expires_ns);
            }
#endif
            if(simulator)simulator->after_event(e,book);
            intents.refill(logical_time);
            if(!simulator&&book.tradable(e.locate)) {
                const auto quote=trading::price_quote(book.top(e.locate),0,pricing);
                if(quote) {
                    ++decisions;mix(decision_hash,message_count);mix(decision_hash,e.locate);
                    mix(decision_hash,static_cast<std::uint64_t>(quote->bid));mix(decision_hash,static_cast<std::uint64_t>(quote->ask));
                    mix(decision_hash,quote->bid_size);mix(decision_hash,quote->ask_size);
                    // Count an independent research intent for each side. These
                    // are NOT broker orders; no risk reservation is falsely
                    // marked acknowledged or filled by this market-only runner.
                    for(int side=0;side<2;++side){if(intents.take())++accepted;else ++throttled;}
                }
            }
        }
        if(input.bad())throw trading::ProtocolError("input read failure");
        if(!message_count)throw trading::ProtocolError("capture contains no messages");
        book.validate();
#ifdef TRADING_ML_CLI
        if(signals)signals->finish();
        if(exporter)exporter->commit();
#endif
        if(simulator){decisions=simulator->stats().decisions;decision_hash=simulator->decision_checksum();}
        auto checksum=book.checksum();mix(checksum,decision_hash);
        std::size_t eligible=0;for(const auto& [id,b]:book.symbols()){(void)b;if(book.tradable(id))++eligible;}
        const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-started).count();
        std::cout<<"{\"schema_version\":1,\"messages\":"<<message_count
            <<",\"symbols\":"<<book.symbols().size()<<",\"decisions\":"<<decisions
            <<",\"trades\":"<<book.trade_count()<<",\"book_valid\":"<<(book.initialized()?"true":"false")
            <<",\"last_sequence\":"<<message_count<<",\"elapsed_ns\":"<<elapsed
            <<",\"checksum\":\""<<hex(checksum)<<"\",\"summary\":{"
            <<"\"mode\":\""<<(simulator?"simulation":"market_data_research")<<"\",\"orders_sent\":0,\"fills_simulated\":"<<(simulator?simulator->stats().fill_count:0)<<",\"pnl\":null,"
            <<"\"book_implementation\":\"reference_tree_and_hash\",\"transport_sequence_available\":false,"
            <<"\"last_sequence_kind\":\"file_message_ordinal\",\"session_start_seen\":"<<(book.initialized()?"true":"false")
            <<",\"trading_eligible_symbols\":"<<eligible<<",\"open_display_orders\":"<<book.order_count()
            <<",\"reported_trade_volume\":"<<book.reported_volume()<<",\"input_bytes\":"<<bytes_read
            <<",\"rate_admitted_intents\":"<<accepted<<",\"rate_rejected_intents\":"<<throttled
            <<",\"decision_checksum\":\""<<hex(decision_hash)<<"\",\"account_inventory\":null,"
            <<"\"inventory_and_pnl_limits_exercised\":"<<((simulator&&simulator->stats().fill_count)?"true":"false")<<",\"effective_risk_parameters\":{"
            <<"\"max_order_size\":"<<options.max_order_size<<",\"soft_inventory_limit\":"<<options.soft_inventory
            <<",\"hard_inventory_limit\":"<<options.hard_inventory<<",\"daily_loss_limit_cents\":"<<options.daily_loss_cents
            <<",\"orders_per_second\":"<<options.orders_per_second<<",\"tick_size\":"<<options.tick
            <<"},\"pricing_economics\":{\"policy_version\":\""<<trading::quote_economics_policy_version
            <<"\",\"reference\":\"inventory_skewed_reservation_price\",\"fee_per_share_units\":"<<options.fee_units
            <<",\"min_fee_per_order_units\":"<<options.min_fee_units<<",\"slippage_buffer_units\":"<<options.slippage_units
            <<",\"min_net_edge_units\":"<<options.net_edge_units<<",\"cost_half_spread_floor_units\":"<<trading::quote_cost_floor(pricing)
            <<",\"minimum_commission_allocation_shares\":1,\"rebates_reduce_quote_floor\":false,"
            <<"\"commission_model\":\"max(min_order_fee, cumulative_filled_quantity * fee_per_share)\"}";
        if(simulator)std::cout<<",\"simulation\":"<<simulator->json();
#ifdef TRADING_ML_CLI
        std::cout<<",\"ml\":{\"market_schema_version\":1,\"signal_schema_version\":1,\"stock_locate\":"<<options.symbol_locate
            <<",\"export_rows\":"<<(exporter?exporter->rows():0)<<",\"signal_rows_consumed\":"<<(signals?signals->consumed():0)
            <<",\"capture_binding_verified_by_engine\":false,\"capture_binding_requirement\":\"Controlled Python caller must verify capture, model and signal SHA256 manifest\","
            <<"\"observations\":\"causal_L1_book_state_not_trade_volume\"}";
#endif
        std::cout<<",\"limitations\":[\"No broker execution or real account PnL; simulation is a model only\","
            <<"\"Length-prefixed ITCH has no transport sequence; gaps cannot be proven absent\","
            <<"\"Default research quotes use zero inventory; simulation uses its synthetic ledger\","
            <<"\"No zero-allocation or latency SLA claim\"]}}\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<"replay failed after "<<message_count<<" messages: "<<error.what()<<'\n';return 2;
    }
}
