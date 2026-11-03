#pragma once
#include "trading/book.hpp"
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <optional>

namespace trading {
inline constexpr char market_export_header[]="ordinal,timestamp_ns,logical_timestamp_ns,stock_locate,bid,ask,bid_qty,ask_qty,valid,tradable";
inline constexpr char signal_file_header[]="ordinal,allow_bid,allow_ask,expires_ns";
struct SideSignal {
    std::uint64_t ordinal{};
    bool allow_bid{},allow_ask{};
    std::uint64_t expires_ns{};
};
// Raw observations only. No labels, future aggregation, or time-batch coalescing.
// The final file is published atomically only after full capture validation.
class MarketExporter {
    std::filesystem::path target_,temporary_;
    int descriptor_{-1};
    Top previous_;
    bool observed_{false},previous_valid_{false},previous_tradable_{false},committed_{false};
    std::uint64_t rows_{0};
    void write_line(const std::string&);
public:
    explicit MarketExporter(const std::filesystem::path&);
    ~MarketExporter();
    MarketExporter(const MarketExporter&)=delete;
    MarketExporter& operator=(const MarketExporter&)=delete;
    void observe(const Event&,const Book&,std::uint16_t locate,std::uint64_t ordinal,std::uint64_t logical_time);
    void commit();
    std::uint64_t rows() const noexcept{return rows_;}
};
// Streaming, strictly ordered signals. Identity/hash binding to the source
// capture and trained model is required in the controlled Python caller.
class SignalReader {
    std::ifstream input_;
    std::optional<SideSignal> next_;
    std::uint64_t previous_ordinal_{0},consumed_{0};
    bool read_line(std::string&);
    void advance();
public:
    explicit SignalReader(const std::filesystem::path&);
    std::optional<SideSignal> at(std::uint64_t ordinal);
    void finish() const;
    std::uint64_t consumed() const noexcept{return consumed_;}
};
}
