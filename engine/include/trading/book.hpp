#pragma once
#include "trading/itch.hpp"
#include <map>
#include <unordered_map>
#include <optional>
#include <vector>

namespace trading {
struct BookOrder { std::uint16_t locate; Side side; Price price; std::uint64_t leaves; };
struct Level { std::uint64_t quantity{}; std::uint64_t orders{}; };
struct Top { Price bid{},ask{}; std::uint64_t bid_qty{},ask_qty{}; bool valid{}; };
struct SymbolBook {
    std::string symbol;
    std::map<Price,Level> bids,asks;
    char trading_status{'?'};
    bool operational_halt{false};
    Top top() const noexcept;
};
struct TradeRecord { std::uint16_t locate; std::uint64_t quantity; Price price; bool printable; bool broken; };
class Book {
    std::unordered_map<RefId,BookOrder> orders_;
    std::map<std::uint16_t,SymbolBook> symbols_;
    std::unordered_map<std::uint64_t,TradeRecord> trades_;
    std::size_t capacity_;
    std::size_t trade_capacity_;
    bool start_seen_{false}, market_open_{false}, end_seen_{false};
    std::uint64_t reported_volume_{0};
    void add(const Event&);
    BookOrder reduce(RefId,std::uint16_t,std::uint64_t);
    void record_trade(const Event&,Price);
public:
    explicit Book(std::size_t max_orders=2'000'000,std::size_t max_trades=2'000'000);
    void apply(const Event&);
    const auto& symbols() const noexcept { return symbols_; }
    std::size_t order_count() const noexcept { return orders_.size(); }
    std::size_t trade_count() const noexcept { return trades_.size(); }
    std::uint64_t reported_volume() const noexcept { return reported_volume_; }
    bool initialized() const noexcept { return start_seen_; }
    bool tradable(std::uint16_t) const noexcept;
    Top top(std::uint16_t) const noexcept;
    std::optional<BookOrder> order(RefId) const noexcept;
    std::vector<std::pair<RefId,std::uint64_t>> queue_at(std::uint16_t,Side,Price) const;
    void validate() const;
    std::uint64_t checksum() const;
};
}
