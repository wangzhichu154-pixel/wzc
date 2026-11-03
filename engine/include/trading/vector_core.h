#ifndef VECTOR_CORE_C_ABI_H
#define VECTOR_CORE_C_ABI_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define VC_ABI_VERSION 1u
typedef struct vc_handle vc_handle;
typedef enum vc_result {
    VC_OK=0, VC_INVALID_ARGUMENT=1, VC_NOT_READY=2, VC_RISK_REJECTED=3,
    VC_DUPLICATE=4, VC_RECONCILIATION_REQUIRED=5, VC_INTERNAL_ERROR=6
} vc_result;
typedef enum vc_fault_reason {
    VC_FAULT_NONE=0, VC_FAULT_MANUAL=1, VC_FAULT_FEED=2, VC_FAULT_BROKER=3,
    VC_FAULT_UNKNOWN_ORDER=4, VC_FAULT_EXECUTION_CORRECTION=5,
    VC_FAULT_PROTOCOL=6, VC_FAULT_CLOCK=7, VC_FAULT_LOSS=8, VC_FAULT_SYSTEM=9
} vc_fault_reason;

/* Fixed-width ABI. Price/money: 1/10000 USD. Side: 0 buy, 1 sell.
 * All calls for one handle MUST occur on its creating thread. This is a
 * correctness/reference engine, not an independently accepted live HFT core.
 */
typedef struct vc_config {
    uint32_t abi_version;
    uint32_t struct_size;
    uint32_t symbol_locate;
    uint32_t max_order_size;
    int64_t hard_inventory_limit;
    uint64_t max_notional_units;
    uint64_t max_open_orders;
    int64_t daily_loss_limit_units;
    int64_t daily_drawdown_limit_units;
    uint64_t max_market_age_ns;
    uint64_t orders_per_second;
    int64_t price_low;
    int64_t price_high;
    int64_t tick_size;
    uint64_t max_book_orders;
    uint64_t max_trade_records;
    char symbol[9];
    uint8_t reserved[7];
} vc_config;

typedef struct vc_health {
    uint8_t broker_healthy;
    uint8_t system_healthy;
    uint8_t feed_connected;
    uint8_t sequence_complete;
    uint8_t live_source;
    uint8_t reserved[3];
} vc_health;

typedef struct vc_quote {
    int64_t bid_price;
    int64_t ask_price;
    uint32_t bid_quantity;
    uint32_t ask_quantity;
    int64_t market_bid;
    int64_t market_ask;
    uint64_t market_bid_quantity;
    uint64_t market_ask_quantity;
    uint64_t last_valid_bbo_ns;
    uint8_t market_fresh;
    uint8_t trading_status_valid;
    uint8_t quote_available;
    uint8_t reserved[5];
} vc_quote;

typedef struct vc_permit {
    uint64_t reservation_id;
    uint64_t risk_epoch;
    uint64_t deadline_ns;
} vc_permit;

typedef struct vc_snapshot {
    uint64_t risk_epoch;
    int64_t position;
    uint64_t buy_reserved;
    uint64_t sell_reserved;
    uint64_t open_orders;
    int64_t net_pnl_units;
    uint32_t reason;
    uint32_t last_risk_reject;
    uint8_t killed;
    uint8_t armed;
    uint8_t reconcile_required;
    uint8_t session_complete;
    uint8_t pnl_mark_valid;
    uint8_t reserved[3];
} vc_snapshot;

int vc_create(const vc_config* config, vc_handle** output);
int vc_destroy(vc_handle* handle);
int vc_last_error(vc_handle* handle, char* output, uint32_t capacity);
/* Required startup configuration, additive to ABI version 1; existing structs
 * are unchanged. Each field is 0..1e9 money units; sum at most 4e9. The complete
 * order minimum is allocated to one share in the conservative quote floor.
 * Only before first successful arm, with no reserved orders. Immutable after
 * first arm even if a later kill makes armed=false. Invalid values are atomic
 * rejections; no partial configuration change. Rebates never narrow quotes.
 */
int vc_set_quote_economics(vc_handle* handle,int64_t fee_per_share_units,
    int64_t min_fee_per_order_units,int64_t slippage_buffer_units,int64_t min_net_edge_units);
int vc_process_itch(vc_handle* handle,const uint8_t* message,uint32_t size,uint64_t receive_mono_ns);
int vc_set_health(vc_handle* handle,const vc_health* health);
int vc_get_quote(vc_handle* handle,uint64_t now_ns,vc_quote* output);
int vc_get_snapshot(vc_handle* handle,vc_snapshot* output);

/* Empty and flat are mandatory. Call only after all broker reconciliation end
 * markers, account identity verification and complete live-feed sequencing.
 * Existing holdings/order bootstrap and automatic kill reset are unsupported.
 */
int vc_arm_flat(vc_handle* handle,uint8_t paper_account_verified,
    uint8_t reconciliation_complete,uint64_t broker_open_orders,
    int64_t broker_position,uint64_t now_ns);
int vc_check_and_reserve(vc_handle* handle,uint64_t order_id,uint8_t side,
    uint32_t quantity,int64_t price,uint64_t now_ns,uint64_t deadline_ns,vc_permit* output);
/* Atomically consumes the one-time send permit after a second safety check.
 * An SDK exception AFTER this call means outcome unknown unless non-send is
 * independently proven. Do not retry the same economic order automatically.
 */
int vc_send_boundary(vc_handle* handle,const vc_permit* permit,uint64_t now_ns);
int vc_ack(vc_handle* handle,uint64_t order_id);
int vc_fill(vc_handle* handle,uint64_t execution_id,uint64_t order_id,uint32_t quantity,int64_t price);
int vc_cancel_request(vc_handle* handle,uint64_t order_id);
int vc_cancel_confirm(vc_handle* handle,uint64_t order_id);
int vc_reject(vc_handle* handle,uint64_t order_id);
int vc_definitely_not_sent(vc_handle* handle,uint64_t order_id);
int vc_outcome_unknown(vc_handle* handle,uint64_t order_id);
int vc_execution_correction(vc_handle* handle);
/* Caller maintains a reconciled FIFO/cash ledger; fill updates C++ position and
 * reservation only. Every NEW fill invalidates PnL freshness until this call.
 * Fees/rebates must be nonnegative totals; do not pass deltas here.
 */
int vc_update_pnl(vc_handle* handle,int64_t realized,int64_t unrealized,
    int64_t fees,int64_t rebates,uint8_t mark_valid);
int vc_kill(vc_handle* handle,uint32_t reason);

#ifdef __cplusplus
}
#endif
#endif
