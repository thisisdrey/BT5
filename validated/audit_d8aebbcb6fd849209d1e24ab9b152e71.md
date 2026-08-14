### Title
`close_account` allows closing accounts with sub-threshold asset dust, permanently stranding those shares in the bank's global share pool - (File: `programs/marginfi/src/instructions/marginfi_account/close.rs`)

### Summary
`close_account` gates closure on `MarginfiAccount::can_be_closed()`, which treats any balance whose `asset_shares` is below `EMPTY_BALANCE_THRESHOLD` as "empty" via `Balance::get_side()`, even though those shares are still nonzero and still counted in the bank's `total_asset_shares`. Because `close_account` never calls the balance-unwind logic that `lending_account_close_balance` uses (which explicitly decrements `bank.total_asset_shares` and routes leftover dust to `collected_insurance_fees_outstanding`), an account with only dust-sized residual asset shares can be closed while those shares remain phantom entries in the bank, permanently unclaimable once the owning account is deleted.

### Finding Description
`can_be_closed()` computes eligibility per balance as: [1](#0-0) 

It relies on `balance.get_side().is_none()` for the asset check, but `get_side()` only returns `None` when **both** `asset_shares` and `liability_shares` are below `EMPTY_BALANCE_THRESHOLD` — it does not require the shares to be exactly zero: [2](#0-1) 

By contrast, the liability side in `can_be_closed` is checked with `liability_shares <= I80F48::ZERO` (strict, no tolerance), so liability dust correctly blocks closure. Asset dust does not.

`close_account` itself performs no per-balance unwind — it only checks flags/records/orders and then `can_be_closed()`, and lets Anchor's `close = fee_payer` destroy the account: [3](#0-2) 

Compare this to the balance-level close path, `lending_account_close_balance`, which explicitly unwinds the position from the bank and routes any leftover asset dust to `collected_insurance_fees_outstanding` before deactivating the balance: [4](#0-3) 

That function's comments make the intended invariant explicit: any dust asset amount still in the vault must be routed to `collected_insurance_fees_outstanding` "so vault content stays fully accounted for" [5](#0-4) . `close_account` never calls `close_balance`, so if a balance still holds sub-threshold `asset_shares` (`active == 1`), the account can be destroyed directly without that unwind ever running: `bank.total_asset_shares` is never decremented, and the dust value is never routed to insurance fees. The dust shares remain counted in `bank.total_asset_shares` forever, tied to an owner that no longer exists.

### Impact Explanation
This is a real accounting desync: the bank continues to report `total_asset_shares` that include shares belonging to a destroyed account, meaning that value is permanently unclaimable by anyone (not the original owner, not the protocol via insurance-fee routing, not other depositors). It dilutes/depresses effective share value for remaining depositors by a small, permanent amount per occurrence. The magnitude of each individual occurrence is bounded by `EMPTY_BALANCE_THRESHOLD`, so this does not enable large-scale theft in a single transaction, but it is a genuine violation of "closing an account must never strand value," and is repeatable by any user across many accounts to accumulate stranded/unaccounted value in a bank's books over time.

### Likelihood Explanation
Trivially reachable by any unprivileged account holder: deposit a tiny amount, perform an operation (e.g., a withdrawal near full amount) that leaves `asset_shares` just under `EMPTY_BALANCE_THRESHOLD` without deactivating the balance slot through `close_balance`, then call `close_account`. No admin/governance, no oracle manipulation, and no guard currently blocks it since `can_be_closed()`'s asset-side check only inspects `get_side()`, not the raw shares.

### Recommendation
In `can_be_closed()`, check the raw `asset_shares` against zero (or route through the same dust-unwind path used by `close_balance`) instead of relying on `get_side().is_none()`, which tolerates up to `EMPTY_BALANCE_THRESHOLD` of asset dust. Alternatively, have `close_account` iterate active balances and invoke the same unwind/insurance-fee-routing logic as `lending_account_close_balance` for any residual dust before allowing closure, so `bank.total_asset_shares` is always kept in sync with actual claimable balances.

### Proof of Concept
Rust integration test plan (extend `programs/marginfi/tests/user_actions/close_account.rs`):
1. Create a bank and a marginfi account; deposit a small amount and withdraw all but a sub-`EMPTY_BALANCE_THRESHOLD` amount of `asset_shares` (using `try_bank_withdraw` with `withdraw_all = false`), leaving the balance `active == 1` with dust `asset_shares` but not deactivated via `lending_account_close_balance`.
2. Record `bank.total_asset_shares` before closing.
3. Call `try_close_account` and assert it succeeds (`res.is_ok()`), even though the balance still holds nonzero `asset_shares`.
4. Assert `bank.total_asset_shares` after closure is unchanged from before (i.e., not decremented by the dust amount), and that `bank.collected_insurance_fees_outstanding` was not incremented — proving the dust value is neither returned to a depositor, nor tracked by the protocol, nor discoverable by any future instruction, i.e., permanently stranded.

### Citations

**File:** programs/marginfi/src/state/marginfi_account.rs (L181-196)
```rust
    fn can_be_closed(&self) -> bool {
        let is_disabled = self.get_flag(ACCOUNT_DISABLED);
        let is_in_flashloan = self.get_flag(ACCOUNT_IN_FLASHLOAN);
        let is_in_receivership = self.get_flag(ACCOUNT_IN_RECEIVERSHIP);
        let is_frozen = self.get_flag(ACCOUNT_FROZEN);
        let only_has_empty_balances = self.lending_account.balances.iter().all(|balance| {
            let liability_shares: I80F48 = balance.liability_shares.into();
            balance.get_side().is_none() && liability_shares <= I80F48::ZERO
        });

        !is_disabled
            && only_has_empty_balances
            && !is_in_flashloan
            && !is_in_receivership
            && !is_frozen
    }
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1739-1798)
```rust
    pub fn close_balance(&mut self, in_receivership: bool) -> MarginfiResult<()> {
        let balance = &mut self.balance;
        let bank = &mut self.bank;

        let current_liability_amount =
            bank.get_liability_amount(balance.liability_shares.into())?;
        let current_asset_amount = bank.get_asset_amount(balance.asset_shares.into())?;

        check!(
            current_liability_amount.is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::IllegalBalanceState,
            "Balance has existing debt"
        );

        check!(
            current_asset_amount.is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::IllegalBalanceState,
            "Balance has existing assets"
        );

        let asset_shares: I80F48 = balance.asset_shares.into();
        let liability_shares: I80F48 = balance.liability_shares.into();
        // Counters are incremented in `*_balance_internal` when shares cross
        // `ZERO_AMOUNT_THRESHOLD` upward; match that condition so we don't
        // double-decrement positions that already crossed downward earlier.
        let had_assets = asset_shares.is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD);
        let had_liabs = liability_shares.is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD);

        balance.close()?;

        if in_receivership {
            bank.cache.clear_liquidation_price_cache_locked();
        }

        // Asset-side dust = real tokens still in the liquidity vault that the
        // user never withdrew. Route to `collected_insurance_fees_outstanding`
        // so vault content stays fully accounted for, mirroring the fractional-
        // remainder handling in `withdraw_all`.
        if current_asset_amount > I80F48::ZERO {
            bank.collected_insurance_fees_outstanding =
                I80F48::from(bank.collected_insurance_fees_outstanding)
                    .checked_add(current_asset_amount)
                    .ok_or_else(math_error!())?
                    .into();
        }

        bank.change_asset_shares(-asset_shares, false)?;
        // Liability-side dust = bad debt the borrower never repaid. Decrementing
        // here makes the loss explicit instead of leaving phantom shares in
        // `total_liability_shares` that would compound interest indefinitely.
        bank.change_liability_shares(-liability_shares, true)?;

        if had_assets {
            bank.decrement_lending_position_count();
        }
        if had_liabs {
            bank.decrement_borrowing_position_count();
        }

        Ok(())
```

**File:** type-crate/src/types/user_account.rs (L330-345)
```rust
    pub fn get_side(&self) -> Option<BalanceSide> {
        let asset_shares = I80F48::from(self.asset_shares);
        let liability_shares = I80F48::from(self.liability_shares);

        assert!(
            asset_shares < EMPTY_BALANCE_THRESHOLD || liability_shares < EMPTY_BALANCE_THRESHOLD
        );

        if I80F48::from(self.liability_shares) >= EMPTY_BALANCE_THRESHOLD {
            Some(BalanceSide::Liabilities)
        } else if I80F48::from(self.asset_shares) >= EMPTY_BALANCE_THRESHOLD {
            Some(BalanceSide::Assets)
        } else {
            None
        }
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/close.rs (L6-32)
```rust
pub fn close_account(ctx: Context<MarginfiAccountClose>) -> MarginfiResult {
    let marginfi_account = &ctx.accounts.marginfi_account.load()?;

    if marginfi_account.get_flag(ACCOUNT_FROZEN) {
        return err!(MarginfiError::AccountFrozen);
    }

    check!(
        marginfi_account.liquidation_record == Pubkey::default(),
        MarginfiError::IllegalAction,
        "Close liquidation record before closing account"
    );

    check!(
        marginfi_account.active_orders == 0,
        MarginfiError::IllegalAction,
        "Close all active orders before closing account"
    );

    check!(
        marginfi_account.can_be_closed(),
        MarginfiError::IllegalAction,
        "Account cannot be closed"
    );

    Ok(())
}
```
