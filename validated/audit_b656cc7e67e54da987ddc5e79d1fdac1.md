## Analog Found [1](#0-0) 

### Title
Balance Closure Wipes `emissions_outstanding` Without Requiring Settlement, Permanently Losing Accrued Unclaimed Emissions - ([File: programs/marginfi/src/state/marginfi_account.rs])

### Summary
`Balance::close()` unconditionally overwrites the entire `Balance` struct — including the `emissions_outstanding` field that tracks a user's accrued-but-unpaid emission rewards — with `empty_deactivated()`. The `close_balance`, `withdraw_all`, and `repay_all` paths only check that `asset`/`liability` amounts are zero before invoking `close()`; none of them checks or settles `emissions_outstanding` first. This mirrors the reported bug class: a "position" is judged empty/closeable purely from its principal balances, while it still carries pending, unclaimed value (pending stake in the original report; unclaimed emissions here) that is silently discarded on closure.

### Finding Description
`Balance::close()` resets every field of the balance, including `emissions_outstanding`, to zero: [2](#0-1) [3](#0-2) 

This `close()` call is reached from three unprivileged, user-callable paths, each of which gates on asset/liability amounts only:
- `close_balance` — checks only `current_liability_amount`/`current_asset_amount` are zero before calling `balance.close()`: [4](#0-3) 
- `withdraw_all` — checks only the asset amount is positive and liability is zero, then calls `balance.close()`: [5](#0-4) 
- `repay_all` — checks only liability/asset amounts, then calls `balance.close()`: [6](#0-5) 

None of these three check paths inspects `balance.emissions_outstanding` before wiping it. The codebase does define an error variant seemingly meant to guard exactly this case, `CannotCloseOutstandingEmissions` (error code 6033): [7](#0-6) 
but I could not find any call site in the current instruction/state code (`close_balance.rs`, `withdraw.rs`, `repay.rs`, `marginfi_account.rs`) that actually raises this error — the only occurrences left in the repo are the enum/`From<u32>` definitions and stale test/regression references, consistent with the patch notes stating that related emissions-management instructions (`lending_pool_reclaim_emissions_vault`, `lending_account_clear_emissions`) were removed in a recent "Emissions and Legacy Curve Wind-down": [8](#0-7) 

This is directly analogous to the `LiquidationPool::empty` bug: there, a position was classified "empty" (and thus excluded from a benefit — asset distribution) using only two of its fields (`TST`, `EUROs`), ignoring a third pending-value field (`pendingStake`). Here, a `Balance` is classified "closeable" using only two fields (asset/liability shares), and closing it discards a third pending-value field (`emissions_outstanding`) with no settlement or forfeiture check.

### Impact Explanation
Any depositor/borrower earning emissions on a bank (`EMISSIONS_FLAG_LENDING_ACTIVE` / `EMISSIONS_FLAG_BORROW_ACTIVE`) who accrues rewards and then fully withdraws (`withdraw_all=true`), fully repays (`repay=true`), or explicitly closes the balance (`lending_account_close_balance`) before those emissions are settled/paid out will have `emissions_outstanding` zeroed with no payout — a permanent, unrecoverable loss of the user's own earned rewards. This is a loss-of-funds bug for the account owner, not merely a UX inconvenience, since there is no on-chain path to recover the value once the balance struct is overwritten.

### Likelihood Explanation
The paths (`withdraw_all`, `repay_all`, `close_balance`) are all standard, permissionless, user-authority-gated instructions exercised on every account lifecycle event described in the docs, e.g. "Set `withdraw_all` to true to ignore your amount input and withdraw the entire balance. This is the only way to close a Balance": [9](#0-8) 
Any active emissions campaign combined with a user who withdraws/repays/closes in full before a reward-settlement instruction runs will trigger this loss. Given the emissions system appears to have been actively wound down/refactored recently (per patch notes), it's plausible the removed safety check (`CannotCloseOutstandingEmissions`) was not re-wired into the surviving close paths — this is a real gap, though I could not fully confirm within the available index whether an automatic emissions-settlement step is invoked elsewhere in these instruction handlers (e.g., inside `accrue_interest`/`update_bank_cache`) before `close()` is called; I did not find such a call in `close_balance.rs`, `withdraw.rs`, or `repay.rs`, but I was not able to inspect the full contents of `programs/marginfi/src/instructions/marginfi_account/emissions.rs` before the tool budget ran out, so I cannot rule out that emissions settlement is mandated as a separate transaction step enforced client-side only (which is still bypassable) rather than program-enforced.

### Recommendation
Before calling `balance.close()` in `close_balance`, `withdraw_all`, and `repay_all`, check `balance.emissions_outstanding` (with the same threshold semantics used for asset/liability): if it's non-zero, either (a) settle it (pay/transfer to the user or their `emissions_destination_account`) as part of the same call, or (b) reject the close with `MarginfiError::CannotCloseOutstandingEmissions`, mirroring the guard the error variant already implies. This prevents a position with pending unclaimed value from being incorrectly treated as fully empty and discarded, matching the recommended mitigation pattern used for `pendingStake` in the original report.

### Proof of Concept
1. Admin enables `EMISSIONS_FLAG_LENDING_ACTIVE` on a bank and funds an emissions campaign.
2. User deposits into that bank; over time `balance.emissions_outstanding` accrues to a non-zero value (via whatever mechanism updates it, e.g. deposit/withdraw/borrow/repay actions or a dedicated emissions-update instruction) while the campaign is active but before the user has been paid out.
3. User calls `lending_account_withdraw` with `withdraw_all = true` for the full asset amount (or `lending_account_repay` with `repay = true`, or `lending_account_close_balance` directly).
4. `withdraw_all`/`repay_all`/`close_balance` validate only that the resulting asset/liability amounts are zero, then call `balance.close()`, which sets `emissions_outstanding` to `I80F48::ZERO` via `empty_deactivated()`.
5. No transfer or accounting event compensates the user for the discarded `emissions_outstanding`; the value is permanently lost. [4](#0-3) [3](#0-2)

### Citations

**File:** type-crate/src/types/user_account.rs (L294-306)
```rust
    /// The user's asset (deposit) shares in the bank. Multiply by `bank.asset_share_value` for
    /// the token amount.
    pub asset_shares: WrappedI80F48,
    /// The user's liability (borrow) shares in the bank. Multiply by `bank.liability_share_value`
    /// for the token amount.
    pub liability_shares: WrappedI80F48,
    /// Unclaimed emissions rewards for this position
    pub emissions_outstanding: WrappedI80F48,
    /// Unix timestamp (u64) of the last emissions calculation for this position
    pub last_update: u64,
    /// Reserved for future use
    pub _padding: [u64; 1],
}
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

**File:** programs/marginfi/src/state/marginfi_account.rs (L1485-1489)
```rust
    fn close(&mut self) -> MarginfiResult {
        *self = Self::empty_deactivated();

        Ok(())
    }
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1627-1648)
```rust
    pub fn withdraw_all(&mut self, in_receivership: bool) -> MarginfiResult<(u64, I80F48)> {
        let balance = &mut self.balance;
        let bank = &mut self.bank;

        let total_asset_shares: I80F48 = balance.asset_shares.into();
        let current_asset_amount = bank.get_asset_amount(total_asset_shares)?;
        let current_liability_amount =
            bank.get_liability_amount(balance.liability_shares.into())?;

        debug!("Withdrawing all: {}", current_asset_amount);

        check!(
            current_asset_amount.is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::NoAssetFound
        );

        check!(
            current_liability_amount.is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::NoAssetFound
        );

        balance.close()?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1685-1705)
```rust
    pub fn repay_all(&mut self, in_receivership: bool) -> MarginfiResult<(u64, I80F48)> {
        let balance = &mut self.balance;
        let bank = &mut self.bank;

        let total_liability_shares: I80F48 = balance.liability_shares.into();
        let current_liability_amount = bank.get_liability_amount(total_liability_shares)?;
        let current_asset_amount = bank.get_asset_amount(balance.asset_shares.into())?;

        debug!("Repaying all: {}", current_liability_amount,);

        check!(
            current_liability_amount.is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::NoLiabilityFound
        );

        check!(
            current_asset_amount.is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::NoLiabilityFound
        );

        balance.close()?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1739-1767)
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
```

**File:** programs/marginfi/src/errors.rs (L71-72)
```rust
    #[msg("Cannot close balance because of outstanding emissions")] // 6033
    CannotCloseOutstandingEmissions,
```

**File:** patch-note-drafts/patch-notes-0.1.9.md (L34-38)
```markdown
## Emissions and Legacy Curve Wind-down

The emissions removal begun last release is finished. `lending_pool_reclaim_emissions_vault` and
`lending_account_clear_emissions` are removed. `migrate_curve` was also removed, all banks in the
main group now utilize the new seven-point curve.
```

**File:** guides/DEVELOPERS_INTEGRATORS/GETTING_STARTED_INTEGRATOR.md (L73-75)
```markdown
- Set `withdraw_all` to "true" to ignore your amount input and withdraw the entire balance. This
is the only way to close a Balance so it no longer appears on your Account, simply withdrawing
by configuring `amount` will always leave the Balance on your account, even with zero shares.
```
