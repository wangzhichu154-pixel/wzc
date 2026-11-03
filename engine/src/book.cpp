#include "trading/book.hpp"
#include <limits>
#include <tuple>
#include <vector>
#include <algorithm>

namespace trading {
Top SymbolBook::top() const noexcept {
    if(bids.empty()||asks.empty()) return {};
    const auto& b=*bids.rbegin(); const auto& a=*asks.begin();
    return {b.first,a.first,b.second.quantity,a.second.quantity,b.first<a.first};
}
Book::Book(std::size_t max_orders,std::size_t max_trades):capacity_(max_orders),trade_capacity_(max_trades) {
    if(!capacity_||!trade_capacity_) throw ProtocolError("zero book capacity");
    orders_.reserve(std::min(capacity_,std::size_t{100'000}));
    trades_.reserve(std::min(trade_capacity_,std::size_t{100'000}));
}
void Book::add(const Event& e) {
    if(!e.reference||e.quantity==0||e.price<=0) throw ProtocolError("invalid add");
    if(orders_.contains(e.reference)) throw ProtocolError("duplicate order reference");
    if(orders_.size()>=capacity_) throw ProtocolError("order capacity exhausted");
    auto& book=symbols_[e.locate];
    const auto name=stock_name(e.stock);
    if(!name.empty()) {
        if(!book.symbol.empty()&&book.symbol!=name) throw ProtocolError("stock locate changed identity");
        book.symbol=name;
    }
    auto& ladder=e.side==Side::Buy?book.bids:book.asks;
    auto& level=ladder[e.price];
    if(e.quantity>UINT64_MAX-level.quantity) throw ProtocolError("price level overflow");
    level.quantity+=e.quantity; ++level.orders;
    orders_.emplace(e.reference,BookOrder{e.locate,e.side,e.price,e.quantity});
}
BookOrder Book::reduce(RefId ref,std::uint16_t locate,std::uint64_t quantity) {
    auto it=orders_.find(ref);
    if(it==orders_.end()) throw ProtocolError("unknown order reference; incomplete or corrupt capture");
    const auto original=it->second;
    if(original.locate!=locate) throw ProtocolError("order stock locate mismatch");
    if(quantity==0||quantity>original.leaves) throw ProtocolError("order over-reduction");
    auto& book=symbols_.at(locate);
    auto& ladder=original.side==Side::Buy?book.bids:book.asks;
    auto level=ladder.find(original.price);
    if(level==ladder.end()||level->second.quantity<quantity) throw ProtocolError("inconsistent level");
    level->second.quantity-=quantity; it->second.leaves-=quantity;
    if(it->second.leaves==0) { --level->second.orders; orders_.erase(it); }
    if(level->second.quantity==0) {
        if(level->second.orders!=0) throw ProtocolError("empty level retains orders");
        ladder.erase(level);
    }
    return original;
}
void Book::record_trade(const Event& e,Price price) {
    if(trades_.contains(e.match)) throw ProtocolError("duplicate match number");
    if(trades_.size()>=trade_capacity_) throw ProtocolError("trade ledger capacity exhausted");
    if(e.printable && e.quantity>UINT64_MAX-reported_volume_) throw ProtocolError("trade volume overflow");
    trades_.emplace(e.match,TradeRecord{e.locate,e.quantity,price,e.printable,false});
    if(e.printable) reported_volume_+=e.quantity;
}
void Book::apply(const Event& e) {
    if(e.type=='R'||e.type=='H'||e.type=='h') {
        const auto name=stock_name(e.stock);
        const auto known=symbols_.find(e.locate);
        if(!name.empty()&&known!=symbols_.end()&&!known->second.symbol.empty()&&known->second.symbol!=name)
            throw ProtocolError("status stock locate identity mismatch");
    }
    switch(e.type) {
    case 'S':
        if(e.status=='O') {
            if(start_seen_||!orders_.empty()||!trades_.empty()) throw ProtocolError("unexpected session restart");
            start_seen_=true;
        } else if(e.status=='Q') {
            if(end_seen_) throw ProtocolError("market reopened after end of session");
            market_open_=true;
        } else if(e.status=='M'||e.status=='E'||e.status=='C') {
            market_open_=false; if(e.status=='C') end_seen_=true;
        }
        break;
    case 'R': {
        auto& b=symbols_[e.locate]; const auto name=stock_name(e.stock);
        if(name.empty()||(!b.symbol.empty()&&b.symbol!=name)) throw ProtocolError("invalid stock directory identity");
        b.symbol=name;break;
    }
    case 'H':
        if(e.status!='H'&&e.status!='P'&&e.status!='Q'&&e.status!='T') throw ProtocolError("invalid trading status");
        symbols_[e.locate].trading_status=e.status;break;
    case 'h':
        if(e.status!='H'&&e.status!='T') throw ProtocolError("invalid operational halt");
        symbols_[e.locate].operational_halt=e.status=='H';break;
    case 'A': case 'F': add(e);break;
    case 'X': reduce(e.reference,e.locate,e.quantity);break;
    case 'D': {
        const auto it=orders_.find(e.reference);
        if(it==orders_.end()) throw ProtocolError("delete unknown order");
        reduce(e.reference,e.locate,it->second.leaves);break;
    }
    case 'U': {
        const auto it=orders_.find(e.reference);
        if(it==orders_.end()) throw ProtocolError("replace unknown order");
        if(e.new_reference==0||e.new_reference==e.reference||orders_.contains(e.new_reference))
            throw ProtocolError("replace duplicate or invalid new reference");
        const auto old=it->second;
        Event replacement=e; replacement.reference=e.new_reference; replacement.side=old.side;
        replacement.stock={};
        reduce(e.reference,e.locate,old.leaves);add(replacement);break;
    }
    case 'E': case 'C': {
        if(trades_.contains(e.match)) throw ProtocolError("duplicate execution match");
        const auto old=reduce(e.reference,e.locate,e.quantity);
        record_trade(e,e.type=='C'?e.price:old.price);break;
    }
    case 'P': case 'Q': record_trade(e,e.price);break;
    case 'B': {
        auto it=trades_.find(e.match);
        if(it==trades_.end()) throw ProtocolError("broken trade missing match");
        if(it->second.broken) throw ProtocolError("duplicate broken trade");
        it->second.broken=true;
        if(it->second.printable) reported_volume_-=it->second.quantity;
        break;
    }
    default: break;
    }
}
bool Book::tradable(std::uint16_t locate) const noexcept {
    const auto it=symbols_.find(locate);
    return start_seen_&&market_open_&&!end_seen_&&it!=symbols_.end()&&
        it->second.trading_status=='T'&&!it->second.operational_halt&&it->second.top().valid;
}
Top Book::top(std::uint16_t locate) const noexcept {
    const auto it=symbols_.find(locate);return it==symbols_.end()?Top{}:it->second.top();
}
std::optional<BookOrder> Book::order(RefId id) const noexcept {
    const auto it=orders_.find(id);if(it==orders_.end())return std::nullopt;return it->second;
}
std::vector<std::pair<RefId,std::uint64_t>> Book::queue_at(std::uint16_t locate,Side side,Price price) const {
    std::vector<std::pair<RefId,std::uint64_t>> result;
    for(const auto& [id,o]:orders_)if(o.locate==locate&&o.side==side&&o.price==price)result.emplace_back(id,o.leaves);
    std::sort(result.begin(),result.end());return result;
}
void Book::validate() const {
    std::map<std::tuple<std::uint16_t,Side,Price>,Level> reconstructed;
    for(const auto& [id,o]:orders_) {
        (void)id;
        if(!o.leaves) throw ProtocolError("zero leaves order retained");
        auto& l=reconstructed[{o.locate,o.side,o.price}]; l.quantity+=o.leaves;++l.orders;
    }
    std::size_t levels=0;
    for(const auto& [locate,b]:symbols_) {
        for(const auto side:{Side::Buy,Side::Sell}) {
            const auto& ladder=side==Side::Buy?b.bids:b.asks;
            for(const auto& [price,l]:ladder) {
                const auto it=reconstructed.find({locate,side,price});
                if(it==reconstructed.end()||it->second.quantity!=l.quantity||it->second.orders!=l.orders)
                    throw ProtocolError("book aggregate invariant violated");
                ++levels;
            }
        }
    }
    if(levels!=reconstructed.size()) throw ProtocolError("orphan order level");
}
std::uint64_t Book::checksum() const {
    std::uint64_t hash=14695981039346656037ULL;
    auto mix=[&](std::uint64_t value){ for(int i=0;i<8;++i){hash^=value&255U;hash*=1099511628211ULL;value>>=8;} };
    std::vector<RefId> ids;ids.reserve(orders_.size());
    for(const auto& [id,o]:orders_){(void)o;ids.push_back(id);}std::sort(ids.begin(),ids.end());
    for(const auto id:ids){const auto& o=orders_.at(id);mix(id);mix(o.locate);mix(static_cast<unsigned>(o.side));mix(static_cast<std::uint64_t>(o.price));mix(o.leaves);}
    for(const auto& [id,b]:symbols_){mix(id);for(auto c:b.symbol)mix(static_cast<unsigned char>(c));mix(static_cast<unsigned char>(b.trading_status));mix(b.operational_halt);}
    ids.clear(); for(const auto& [id,t]:trades_){(void)t;ids.push_back(id);} std::sort(ids.begin(),ids.end());
    for(auto id:ids){const auto&t=trades_.at(id);mix(id);mix(t.locate);mix(t.quantity);mix(static_cast<std::uint64_t>(t.price));mix(t.printable);mix(t.broken);}
    mix(start_seen_);mix(market_open_);mix(end_seen_);return hash;
}
}
