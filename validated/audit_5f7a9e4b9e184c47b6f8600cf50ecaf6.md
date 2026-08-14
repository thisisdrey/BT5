## Analysis: Group-level rate limiter bypass via batched/rapid withdrawals (stale read-only check)

### Title
Group-level rate limit and deleverage withdrawal cap can be bypassed by batching multiple withdraw/borrow instructions against a stale, unmutated counter - (File: `programs/marginfi/src/utils/general.rs`, `programs/marginfi/src/state/marginfi_group.rs`)

### Summary
`record_withdrawal_outflow` enforces the group-level (USD) rate limit as a **read-only check** against `group.rate_limiter`, without ever mutating that counter during the user's withdraw/borrow instruction. The actual counter update only happens later, out-of-band, when the `delegate_flow_admin` calls `update_group_rate_limiter`/`update_deleverage_withdrawals` with aggregated off-chain data. This is structurally the same root cause as the MagicSpend finding: a resource-limit check performed against a value that is not synchronously decremented/incremented, so multiple operations that are each individually validated against the *same* stale value can be chained (in one transaction or in rapid succession before admin settlement) to blow through the intended cap.

### Finding Description
In `record_withdrawal_outflow`, the bank-level limiter is updated inline (it is safe, because the bank account is already `mut`), but the group-level limiter is only checked, never written: [1](#0-0) 

The same "check without commit" pattern is used for the deleverage daily withdrawal cap in `check_deleverage_withdraw_limit`, which reads a cached `withdrawn_today` value but does not update it — the guide explicitly confirms: *"At this stage, the cached group counter is not yet mutated by the user instruction."* [2](#0-1) 

Both `lending_account_withdraw` and `lending_account_borrow` (and the Kamino/Solend/Drift/JupLend withdraw variants) call this same read-only check each time: [3](#0-2) [4](#0-3) 

Because the counter used for the check is not updated between calls, an attacker (or a set of colluding/uncoordinated users) can issue any number of withdraw/borrow instructions — across different banks or different marginfi accounts in the same group — within a single transaction, or across many transactions inside the up-to-~10-minute admin settlement lag window (`MAX_DELEVERAGE_WITHDRAW_LIMIT_UPDATE_LAG_SLOTS = 1_500` slots), each evaluated against the identical `effective_remaining_capacity`/`withdrawn_today` snapshot: [5](#0-4) 

Each individual instruction passes the "remaining capacity" check because none of them observe each other's effect, exactly the batching flaw described in the MagicSpend report (validation loop checks a shared resource that execution actually consumes, without decrementing it between checks).

### Impact Explanation
The group rate limiter and deleverage withdrawal cap exist specifically as defense-in-depth against large, rapid drains of group liquidity — e.g., in response to a compromised risk admin, a misconfigured/compromised oracle, or a mass-liquidation/deleverage event. By batching withdraw/borrow (or deleverage withdraw) instructions within a single transaction or across the settlement lag window, an attacker or a set of accounts can extract far more value than the configured hourly/daily USD cap permits, undetected until the off-chain aggregator eventually reconciles — by which point the funds are already gone. This defeats an explicit security control ("a defense if the risk workflow is abused or compromised") and can directly contribute to bank insolvency/bad debt if the limit was relied upon to bound loss during a crisis window.

### Likelihood Explanation
This is reachable by any unprivileged user: `lending_account_withdraw`/`lending_account_borrow` require no special privilege and can be freely composed into a single transaction (bounded only by compute budget and the number of banks/accounts a user controls). The design is documented as intentional ("Group-level rate limiting is checked read-only during user actions, then settled later from aggregated events"), meaning the gap is not a rare edge case but the constant, structural behavior of every group-limited withdraw/borrow until the next admin settlement — a window that can be up to ~1500 slots (~10 minutes) per the enforced staleness bound.

### Recommendation
Either (a) make the group-level counter updated synchronously/atomically the same way the bank-level counter is (accepting the cost of making the group account `mut` on every rate-limited instruction), or (b) if the async/aggregated design must be kept for account-write-cost reasons, add a hard per-transaction/per-instruction cap (independent of the stale group counter) so that a single transaction cannot exceed some bounded fraction of the group limit — analogous to Coinbase's mitigation of introducing a `maxWithdrawDenominator` to probabilistically bound the maximum single-shot bypass. At minimum, track and check outflow already observed within the *current unsettled transaction* (a transient in-instruction-context tally) in addition to the stale on-chain snapshot.

### Proof of Concept
1. Admin configures `group.rate_limiter.hourly.max_outflow` (USD) to some value `L`, and/or `deleverage_withdraw_window_cache.daily_limit = L`.
2. Attacker builds a single transaction containing `N` `lending_account_withdraw`/`lending_account_borrow` instructions (across different banks or accounts they control), each withdrawing an amount whose USD value is just under `L` (or under `L - withdrawn_today`).
3. For each instruction, `record_withdrawal_outflow` (`programs/marginfi/src/utils/general.rs:483-511`) calls `group.rate_limiter.hourly.effective_remaining_capacity(...)`, which reads the *same* un-mutated `group.rate_limiter` state for every instruction in the tx, since nothing in the withdraw/borrow path calls `try_record_outflow` on the group limiter.
4. All `N` instructions pass individually, and the transaction succeeds, resulting in total outflow of `N * (L - ε)`, far exceeding the intended cap `L`, before the `delegate_flow_admin`'s next `update_group_rate_limiter`/`update_deleverage_withdrawals` call ever observes the excess.

### Citations

**File:** programs/marginfi/src/utils/general.rs (L476-511)
```rust
    // Rate limiting tracks net outflow; skip for flashloan/liquidation/deleverage flows.
    if !should_skip_rate_limit(marginfi_account.account_flags) {
        if bank.rate_limiter.is_enabled() {
            bank.rate_limiter
                .try_record_outflow(native_amount, clock.unix_timestamp)?;
        }

        // Group-level rate limiting: read-only validation + event emission.
        // The admin aggregates events off-chain and calls update_group_rate_limiter.
        if group_rate_limit_enabled {
            check!(price > I80F48::ZERO, MarginfiError::InvalidRateLimitPrice);

            let value = calc_value(
                I80F48::from_num(balance_amount),
                price,
                bank.get_balance_decimals(),
                None,
            )?;
            if group.rate_limiter.hourly.is_enabled() {
                let remaining = group
                    .rate_limiter
                    .hourly
                    .effective_remaining_capacity(clock.unix_timestamp);
                if value.to_num::<i64>() > remaining {
                    return Err(MarginfiError::GroupHourlyRateLimitExceeded.into());
                }
            }
            if group.rate_limiter.daily.is_enabled() {
                let remaining = group
                    .rate_limiter
                    .daily
                    .effective_remaining_capacity(clock.unix_timestamp);
                if value.to_num::<i64>() > remaining {
                    return Err(MarginfiError::GroupDailyRateLimitExceeded.into());
                }
            }
```

**File:** programs/marginfi/src/state/marginfi_group.rs (L236-256)
```rust
    fn check_deleverage_withdraw_limit(
        &self,
        withdrawn_equity: I80F48,
        current_timestamp: i64,
    ) -> MarginfiResult {
        let projected =
            self.projected_deleverage_withdrawn_today(withdrawn_equity, current_timestamp);

        if self.deleverage_withdraw_window_cache.daily_limit != 0
            && projected > self.deleverage_withdraw_window_cache.daily_limit
        {
            msg!(
                "trying to withdraw more than daily limit: {} > {}",
                projected,
                self.deleverage_withdraw_window_cache.daily_limit
            );
            return err!(MarginfiError::DailyWithdrawalLimitExceeded);
        }

        Ok(())
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L146-174)
```rust
        record_withdrawal_outflow(
            group_rate_limit_enabled,
            amount_pre_fee,
            amount_pre_fee,
            price,
            &mut bank,
            &group,
            marginfi_group_loader.key(),
            bank_loader.key(),
            &marginfi_account,
            &clock,
        )?;
        // Note: we only care about the withdraw limit in case of deleverage
        if marginfi_account.get_flag(ACCOUNT_IN_DELEVERAGE) {
            let withdrawn_equity = calc_value(
                I80F48::from_num(amount_pre_fee),
                price,
                bank.get_balance_decimals(),
                None,
            )?;
            group.check_deleverage_withdraw_limit(withdrawn_equity, clock.unix_timestamp)?;
            emit!(DeleverageWithdrawFlowEvent {
                group: marginfi_group_loader.key(),
                bank: bank_loader.key(),
                mint: bank.mint,
                outflow_usd: withdrawn_equity.to_num(),
                current_timestamp: clock.unix_timestamp,
            });
        }
```

**File:** programs/marginfi/src/instructions/marginfi_account/borrow.rs (L233-244)
```rust
    record_withdrawal_outflow(
        group_rate_limit_enabled,
        amount_pre_fee,
        amount_pre_fee,
        rate_limit_price,
        &mut bank,
        &group,
        marginfi_group_loader.key(),
        bank_pk,
        &marginfi_account,
        &clock,
    )?;
```

**File:** programs/marginfi/src/instructions/marginfi_group/update_deleverage_withdrawals.rs (L1-21)
```rust
use crate::{check, state::marginfi_group::MarginfiGroupImpl, MarginfiError, MarginfiResult};
use anchor_lang::prelude::*;
use fixed::types::I80F48;
use marginfi_type_crate::types::MarginfiGroup;

const MAX_DELEVERAGE_WITHDRAW_LIMIT_UPDATE_LAG_SLOTS: u64 = 1_500; // ~10 minutes at ~400ms/slot

/// (delegate_flow_admin only) Update the deleverage daily withdraw outflow.
///
/// The delegate flow admin aggregates `DeleverageWithdrawFlowEvent` events
/// off-chain and calls this instruction at intervals to update the on-chain
/// deleverage daily withdraw outflow.
///
/// This avoids requiring the group account to be writable (mut) in every withdraw instruction.
pub fn update_deleverage_withdrawals(
    ctx: Context<UpdateDeleverageWithdrawals>,
    outflow_usd: u32,
    update_seq: u64,
    event_start_slot: u64,
    event_end_slot: u64,
) -> MarginfiResult {
```
