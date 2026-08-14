## Analysis

The reported bug class is: **a parameter that determines "how much a user actually receives" is fixed by the user at commit time, but the fee/rate used to enforce that guarantee is read live at execution time from a mutable admin-controlled config, so an admin update between commit and execution silently changes the outcome for the user.**

The closest analog in marginfi-v2 is the **limit order (`take_profit`/`stop_loss`) execution flow**, specifically the `order_execution_max_fee` check.

### Root cause

When a user places an order, they lock in `max_slippage` on the `Order` account itself: [1](#0-0) 

But the keeper's maximum allowed "cut" (`order_execution_max_fee`) is **not** stored on the order at placement time. It lives on the global, admin-mutable `FeeState` singleton: [2](#0-1) 

and can be changed at any time by the `global_fee_admin` via `edit_global_fee_state`: [3](#0-2) [4](#0-3) 

At execution time (`end_execute_order`), the *current* value of `fee_state.order_execution_max_fee` — not a value snapshotted when the order was placed — is loaded and used to bound how much of the account's equity the keeper can walk away with: [5](#0-4) 

This value directly weakens the protection the account owner relied on when placing the order: [6](#0-5) 

Because `max_fee_frac = 1 - order_execution_max_fee`, increasing `order_execution_max_fee` *lowers* `allowed_diff = start_health * max_fee_frac`, which *relaxes* the `net >= allowed_diff` check — letting the keeper leave the account owner with less equity than the owner would have gotten under the fee cap in effect when they placed the order. This is structurally identical to the Backed Protocol issue: the user commits to a plan (`minLoanAmount` / take-profit order) under one fee assumption, but the actual outcome is computed later using a fee value the admin can change in the interim, with no re-validation or user consent at that later point.

### Title
Take-profit order payout is bounded by a live, admin-mutable `order_execution_max_fee` instead of the value in effect when the order was placed — (File: `programs/marginfi/src/instructions/marginfi_account/order.rs`)

### Summary
`place_order` locks a user's `max_slippage` into the `Order` account, but the complementary keeper-profit cap, `fee_state.order_execution_max_fee`, is read live from the global `FeeState` at `end_execute_order` rather than being captured on the `Order` at placement time.

### Finding Description
A user places a take-profit (or "Both") order expecting that, at most, `order_execution_max_fee` (as configured when they placed the order) of their equity can be taken by the executing keeper, per the `allowed_diff = start_health * (1 - max_fee)` check in `end_execute_order`. The `global_fee_admin` can call `edit_global_fee_state` to raise `order_execution_max_fee` at any point after the order is placed but before it is triggered/executed. Because `end_execute_order` reads the current `fee_state.order_execution_max_fee` rather than a value stored on the `Order`/`ExecuteOrderRecord` at placement time, the enforced bound silently changes, allowing a keeper to retain a larger cut of the account's equity than what the user agreed to when the order was created.

### Impact Explanation
The account owner (an unprivileged user) can receive materially less asset value on take-profit execution than they intended, with the difference captured by the executing keeper. This is a direct, unauthorized reduction of the value returned to the order owner — the same class of harm as the underlying report (user's realized outcome depends on a fee parameter that can move between commitment and execution, to the user's detriment).

### Likelihood Explanation
Requires only a normal `edit_global_fee_state` admin action (not a compromise) between order placement and keeper execution — a legitimate, expected operational event (fee schedule updates), combined with a pending take-profit order. No malicious/privileged exploit is needed to trigger the mismatch; it's a latent design gap that manifests whenever fee parameters are updated while orders are outstanding.

### Recommendation
Snapshot `order_execution_max_fee` (and any other fee bound relevant to guaranteeing the user's expected payout) onto the `Order` account at `place_order` time, and use that stored value in `end_execute_order` instead of re-reading the live `FeeState`. This mirrors Backed Protocol's adopted fix of locking in the origination fee rate at loan creation.

### Proof of Concept
1. User calls `place_order` creating a take-profit order with `max_slippage` = 1% (accepting that a keeper's execution slippage costs at most ~1%, and implicitly up to the current `order_execution_max_fee`, e.g. 5%, per `FeeState`).
2. Before the trigger condition is met, `global_fee_admin` calls `edit_global_fee_state` raising `order_execution_max_fee` from 5% to, say, 20% (a legitimate config update, e.g. for a different market condition).
3. Price moves and a keeper calls `start_execute_order` / `end_execute_order`. The `max_fee_frac` computed in `end_execute_order` now uses the new 20% cap: [7](#0-6) 
4. The keeper withdraws down to `net == start_health * 0.80` (allowed by the new, higher cap) instead of the `0.95` the user's parameters implied when the order was placed, and the check passes.
5. The account owner receives ~15% less equity than they would have under the fee configuration in effect when they created the order — value that instead accrues to the keeper.

### Citations

**File:** programs/marginfi/src/state/order.rs (L34-48)
```rust
        match trigger {
            OrderTrigger::StopLoss {
                threshold,
                max_slippage,
            } => {
                self.trigger = OrderTriggerType::StopLoss;
                self.stop_loss = threshold;
                self.max_slippage = max_slippage;
                self.take_profit = WrappedI80F48::default();
                // Threshold must be > 0
                let val: I80F48 = self.stop_loss.into();
                check!(
                    val > I80F48::ZERO,
                    MarginfiError::InvalidOrderTakeProfitOrStopLoss
                );
```

**File:** type-crate/src/types/fee_state.rs (L60-63)
```rust
    /// Take-profit Orders can be executed at this premium, which Keepers are allowed to keep (no
    /// pun intended) e.g. (1 + this) * amount repaid >= asset seized
    /// * A percentage    
    pub order_execution_max_fee: WrappedI80F48,
```

**File:** programs/marginfi/src/instructions/marginfi_group/edit_global_fee.rs (L78-87)
```rust
    if let Some(order_execution_max_fee) = order_execution_max_fee {
        let old_f64: f64 = wrapped_i80f48_to_f64(fee_state.order_execution_max_fee);
        let new_f64: f64 = wrapped_i80f48_to_f64(order_execution_max_fee);
        msg!(
            "Updating order_execution_max_fee: {:?} -> {:?}",
            old_f64,
            new_f64
        );
        fee_state.order_execution_max_fee = order_execution_max_fee;
    }
```

**File:** programs/marginfi/src/lib.rs (L603-630)
```rust
    /// (global fee admin only) Adjust fees, admin, wallet, or pause delegate admin
    pub fn edit_global_fee_state(
        ctx: Context<EditFeeState>,
        admin: Option<Pubkey>,
        fee_wallet: Option<Pubkey>,
        bank_init_flat_sol_fee: Option<u32>,
        liquidation_flat_sol_fee: Option<u32>,
        order_init_flat_sol_fee: Option<u32>,
        program_fee_fixed: Option<WrappedI80F48>,
        program_fee_rate: Option<WrappedI80F48>,
        liquidation_max_fee: Option<WrappedI80F48>,
        order_execution_max_fee: Option<WrappedI80F48>,
        pause_delegate_admin: Option<Pubkey>,
    ) -> MarginfiResult {
        marginfi_group::edit_fee_state(
            ctx,
            admin,
            fee_wallet,
            bank_init_flat_sol_fee,
            liquidation_flat_sol_fee,
            order_init_flat_sol_fee,
            program_fee_fixed,
            program_fee_rate,
            liquidation_max_fee,
            order_execution_max_fee,
            pause_delegate_admin,
        )
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/order.rs (L418-446)
```rust
    let max_fee_frac = {
        let max_fee: I80F48 = fee_state.order_execution_max_fee.into();
        I80F48::ONE.checked_sub(max_fee).ok_or_else(math_error!())?
    };

    let start_health = || -> I80F48 { execute_record.order_start_health.into() };

    match order.trigger {
        OrderTriggerType::StopLoss => {
            let sl: I80F48 = order.stop_loss.into();
            let allowed_sl = sl.checked_mul(slippage_frac).ok_or_else(math_error!())?;

            check!(
                net >= allowed_sl,
                MarginfiError::OrderExecutionOverWithdrawal
            );
        }
        OrderTriggerType::TakeProfit => {
            let tp: I80F48 = order.take_profit.into();
            let allowed_tp = tp.checked_mul(slippage_frac).ok_or_else(math_error!())?;

            let allowed_diff = (start_health)()
                .checked_mul(max_fee_frac)
                .ok_or_else(math_error!())?;

            check!(
                net >= allowed_diff && net >= allowed_tp,
                MarginfiError::OrderExecutionOverWithdrawal
            );
```
