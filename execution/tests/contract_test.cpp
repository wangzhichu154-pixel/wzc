#include "trading/execution_gateway.hpp"
#include <iostream>
#include <stdexcept>

using namespace vector_trading::execution;

void require(bool value) {
    if (!value) throw std::runtime_error("execution contract invariant failed");
}

int main() {
    UnavailableGateway gateway;
    OrderRequest order{1, 1, Side::Buy, 10, 1234500, {42, 3, 100}};
    require(gateway.submit(order).status == SubmitStatus::DefinitelyNotSent);
    require(gateway.cancel(1).reason == Rejection::Unavailable);
    require(!gateway.capabilities().new_order);

    GatewayState state{Mode::Paper, true, true, true, true, false, 3};
    require(check_send_boundary(order, state, 99) == Rejection::None);
    require(check_send_boundary(order, state, 100) == Rejection::Expired);
    state.risk_epoch++;
    require(check_send_boundary(order, state, 99) == Rejection::StalePermit);
    state.risk_epoch--;
    state.killed = true;
    require(check_send_boundary(order, state, 99) == Rejection::Killed);
    state.killed = false;
    state.reconciled = false;
    require(check_send_boundary(order, state, 99) == Rejection::ReconciliationRequired);
    state.reconciled = true;
    state.account_mode_verified = false;
    require(check_send_boundary(order, state, 99) == Rejection::UnverifiedAccount);
    state.account_mode_verified = true;
    state.mode = Mode::Live;
    require(check_send_boundary(order, state, 99) == Rejection::LiveDisabled);
    state.mode = Mode::ReadOnly;
    require(check_send_boundary(order, state, 99) == Rejection::ReadOnly);
    state.implementation_available = false;
    require(check_send_boundary(order, state, 99) == Rejection::Unavailable);
    std::cout << "execution safety contract checks passed\n";
}
