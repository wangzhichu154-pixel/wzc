#pragma once
#include "trading/itch.hpp"
#include <functional>
#include <string>
namespace trading {
enum class SequenceStatus { Applied,Duplicate,Gap,Heartbeat,SessionEnd };
struct SequenceResult { SequenceStatus status;std::uint64_t applied;std::uint64_t expected; };
class MoldSequencer {
    std::string session_;
    std::uint64_t expected_;
    bool ended_{false};
public:
    explicit MoldSequencer(std::uint64_t initial_sequence=1):expected_(initial_sequence){}
    SequenceResult receive(std::span<const std::byte>,
        const std::function<void(std::uint64_t,std::span<const std::byte>)>&);
    std::uint64_t expected() const noexcept{return expected_;}
};
}
