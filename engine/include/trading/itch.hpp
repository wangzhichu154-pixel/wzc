#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <string>

namespace trading {
using Price = std::int64_t;
using Qty = std::uint32_t;
using RefId = std::uint64_t;
enum class Side : std::uint8_t { Buy, Sell };
class ProtocolError : public std::runtime_error {
public: using std::runtime_error::runtime_error;
};
struct Event {
    char type{};
    std::uint16_t locate{};
    std::uint64_t timestamp_ns{};
    RefId reference{};
    RefId new_reference{};
    std::uint64_t match{};
    std::uint64_t quantity{};
    Price price{};
    Side side{Side::Buy};
    std::array<char, 8> stock{};
    char status{};
    bool printable{true};
};
std::uint64_t read_be(std::span<const std::byte>, std::size_t offset, std::size_t width);
Event decode_itch(std::span<const std::byte>);
std::string stock_name(const std::array<char, 8>&);
}
