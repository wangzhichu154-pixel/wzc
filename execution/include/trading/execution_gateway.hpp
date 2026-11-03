#pragma once

#include <cstdint>
#include <string_view>

namespace vector_trading::execution {

using OrderId = std::uint64_t;
using MonoNs = std::uint64_t;

enum class Mode { ReadOnly, Paper, Live };
enum class Side { Buy, Sell };
enum class SubmitStatus { DefinitelyNotSent, AcceptedLocally, OutcomeUnknown };
enum class Rejection {
    None, Unavailable, ReadOnly, LiveDisabled, UnverifiedAccount,
    Disconnected, ReconciliationRequired, Killed, StalePermit,
    Expired, InvalidOrder
};

struct RiskPermit {
    std::uint64_t reservation_id{};
    std::uint64_t risk_epoch{};
    MonoNs valid_until{};
};

struct OrderRequest {
    OrderId internal_id{};
    std::uint32_t symbol_id{};
    Side side{Side::Buy};
    std::uint32_t quantity{};
    std::int64_t limit_price_1e4{};
    RiskPermit permit{};
};

struct GatewayState {
    Mode mode{Mode::ReadOnly};
    bool implementation_available{false};
    bool connected{false};
    bool reconciled{false};
    bool account_mode_verified{false};
    bool killed{true};
    std::uint64_t risk_epoch{};
};

struct SubmitResult {
    SubmitStatus status{SubmitStatus::DefinitelyNotSent};
    Rejection reason{Rejection::Unavailable};
};

// This gate is deliberately restrictive. It is an additional send-boundary
// check, NOT a replacement for the synchronous risk engine's reservation.
// A plain RiskPermit is meaningful only inside a trusted same-process boundary.
// Do not accept it from a browser, JSON request, or an untrusted IPC producer.
[[nodiscard]] constexpr Rejection check_send_boundary(
    const OrderRequest& request, const GatewayState& state, MonoNs now) noexcept {
    if (!state.implementation_available) return Rejection::Unavailable;
    if (state.mode == Mode::Live) return Rejection::LiveDisabled;
    if (state.mode != Mode::Paper) return Rejection::ReadOnly;
    if (!state.account_mode_verified) return Rejection::UnverifiedAccount;
    if (!state.connected) return Rejection::Disconnected;
    if (!state.reconciled) return Rejection::ReconciliationRequired;
    if (state.killed) return Rejection::Killed;
    if (!request.permit.reservation_id ||
        request.permit.risk_epoch != state.risk_epoch) return Rejection::StalePermit;
    if (now >= request.permit.valid_until) return Rejection::Expired;
    if (!request.internal_id || !request.symbol_id || !request.quantity ||
        request.limit_price_1e4 <= 0 ||
        (request.side != Side::Buy && request.side != Side::Sell)) {
        return Rejection::InvalidOrder;
    }
    return Rejection::None;
}

struct Capabilities {
    bool read_only_reconciliation{};
    bool new_order{};
    bool native_atomic_replace{};
    bool mass_cancel{};
    bool cancel_on_disconnect{};
    bool exchange_ack_observable{};
};

class IExecutionGateway {
public:
    virtual ~IExecutionGateway() = default;
    [[nodiscard]] virtual Capabilities capabilities() const noexcept = 0;
    [[nodiscard]] virtual SubmitResult submit(const OrderRequest&) noexcept = 0;
    [[nodiscard]] virtual SubmitResult cancel(OrderId) noexcept = 0;
};

// Explicitly unavailable, never an execution simulator or a fake broker.
// No production caller can mistake this result for local acceptance or an ACK.
class UnavailableGateway final : public IExecutionGateway {
public:
    [[nodiscard]] Capabilities capabilities() const noexcept override { return {}; }
    [[nodiscard]] SubmitResult submit(const OrderRequest&) noexcept override {
        return {SubmitStatus::DefinitelyNotSent, Rejection::Unavailable};
    }
    [[nodiscard]] SubmitResult cancel(OrderId) noexcept override {
        return {SubmitStatus::DefinitelyNotSent, Rejection::Unavailable};
    }
};

} // namespace vector_trading::execution
