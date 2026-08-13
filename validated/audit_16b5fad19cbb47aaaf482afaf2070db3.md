### Title
Closing a balance or account with unclaimed emissions permanently forfeits the user's rewards - ([File: programs/marginfi/src/state/marginfi_account.rs])

### Summary
This is a structural analog to the rage-quit finding: in both cases, the protocol allows a user-initiated close/burn action to proceed while a pro-rata reward claim on that same position is still outstanding, permanently destroying the user's ability to claim it.

### Finding Description
Each `Balance` in a marginfi account tracks `emissions_outstanding` — unclaimed emissions rewards accrued on that specific lending/borrowing position: [1](#0-0) 

`BankAccountWrapper::close_balance()`, which backs the `lending_account_close_balance` instruction, closes out a balance's asset/liability shares, decrements position counters, and routes leftover dust to `collected_insurance_fees_outstanding` — but it never reads, checks, zeroes, or otherwise settles `emissions_outstanding`: [2](#0-1) 

`Balance::empty_deactivated()` shows the slot is simply reset (including `emissions_outstanding` back to zero) when the slot is reused, meaning any outstanding value at the time of close is silently lost, not paid out: [3](#0-2) 

Compounding this, `MarginfiAccount::can_be_closed()` — the gate used by `marginfi_account_close` (which fully closes and rent-reclaims the account via `close = fee_payer`) — only checks that balances have no open asset/liability side; it does not check `emissions_outstanding` on any balance: [4](#0-3) [5](#0-4) 

Notably, the error variant `CannotCloseOutstandingEmissions` exists in the error enum ("Cannot close balance because of outstanding emissions") but a repo-wide search shows it is declared and mapped in `errors.rs` yet is never actually raised anywhere in `close_balance.rs`, `close.rs`, or `admin_close.rs` — i.e., the intended guard against this exact scenario is not wired into the instruction logic: [6](#0-5) 

By contrast, the equivalent per-bank guard for the group-level `lending_pool_close_bank` instruction *does* check `emissions_remaining` before allowing a bank to close, showing the protocol is aware such a check belongs here but simply omitted it at the position/account level: [7](#0-6) 

This mirrors the audited PartyGovernanceNFT.rageQuit() issue precisely: a user-triggered destructive action (burn NFT / close balance-close account) races ahead of, and forecloses, a separate pro-rata reward claim mechanism (TokenDistributor.claim() / emissions withdrawal), with no on-chain enforcement requiring the reward to be settled first.

### Impact Explanation
A user who calls `lending_account_close_balance` (or `marginfi_account_close`, which fully deletes the account) while `emissions_outstanding` is nonzero permanently forfeits that reward balance — there is no way to claim emissions once the `Balance` slot is reset/reused or once the `MarginfiAccount` itself is closed and its rent reclaimed. This is a real, concrete loss-of-funds scenario (not theoretical): the position exists specifically to accrue this value, and the value is silently destroyed by a routine, permissionless-to-the-user action with no warning or safeguard in the program itself.

### Likelihood Explanation
Likelihood is comparable to the original finding: it requires either user inattention (forgetting emissions weren't yet withdrawn before calling close) or a race where emissions accrue between a health-check/last state view and the close transaction. Because `close_balance`/`close_account` have no dependency on emissions state and the intended `CannotCloseOutstandingEmissions` check is unused, this can occur in normal usage without any adversarial timing at all — arguably higher likelihood than the original Party Protocol finding, since there it at least required a frontrun of `distribute()`.

### Recommendation
- In `BankAccountWrapper::close_balance()` (and the withdraw-all path), check `balance.emissions_outstanding` and either revert with `MarginfiError::CannotCloseOutstandingEmissions` (the dormant error variant already suggests this was intended) or auto-settle/transfer the outstanding emissions to the user before zeroing the balance.
- Extend `MarginfiAccount::can_be_closed()` to also require `emissions_outstanding == 0` across all balances before permitting `marginfi_account_close`/`admin_close_account`.

### Proof of Concept
1. User deposits into a bank with an active emissions campaign (`EMISSIONS_FLAG_LENDING_ACTIVE` set) and accrues `emissions_outstanding` on their `Balance`.
2. Before calling any emissions-claim instruction, the user (or a bot on their behalf) calls `lending_account_withdraw` with `withdraw_all = true` followed immediately by `lending_account_close_balance`, or directly calls `marginfi_account_close` once all balances are empty.
3. `close_balance()` in `programs/marginfi/src/state/marginfi_account.rs` (lines 1739-1799) executes without ever inspecting `emissions_outstanding`; the slot is later reset via `Balance::empty_deactivated()`, zeroing the field.
4. `can_be_closed()` returns `true` regardless of `emissions_outstanding`, so `marginfi_account_close` succeeds and the account is deleted with rent reclaimed by `fee_payer`.
5. The previously accrued `emissions_outstanding` value is now unrecoverable — there is no code path to claim rewards against a closed balance or a nonexistent account.

### Citations

**File:** type-crate/src/types/user_account.rs (L299-303)
```rust
    pub liability_shares: WrappedI80F48,
    /// Unclaimed emissions rewards for this position
    pub emissions_outstanding: WrappedI80F48,
    /// Unix timestamp (u64) of the last emissions calculation for this position
    pub last_update: u64,
```

**File:** type-crate/src/types/user_account.rs (L347-360)
```rust
    pub fn empty_deactivated() -> Self {
        Balance {
            active: 0,
            bank_pk: Pubkey::default(),
            bank_asset_tag: ASSET_TAG_DEFAULT,
            tag: 0,
            _pad0: [0; 4],
            asset_shares: WrappedI80F48::from(I80F48::ZERO),
            liability_shares: WrappedI80F48::from(I80F48::ZERO),
            emissions_outstanding: WrappedI80F48::from(I80F48::ZERO),
            last_update: 0,
            _padding: [0; 1],
        }
    }
```

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

**File:** programs/marginfi/src/state/marginfi_account.rs (L1737-1799)
```rust
    /// When `in_receivership` is true, clears the bank's liquidation price cache lock
    /// so that banks whose balances are closed mid-liquidation don't stay permanently locked.
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

**File:** programs/marginfi/src/errors.rs (L71-72)
```rust
    #[msg("Cannot close balance because of outstanding emissions")] // 6033
    CannotCloseOutstandingEmissions,
```

**File:** programs/marginfi/src/instructions/marginfi_group/close_bank.rs (L38-41)
```rust
    check!(
        I80F48::from(bank.emissions_remaining).is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
        MarginfiError::BankCannotClose
    );
```
