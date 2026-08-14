### Title
Closing a lending position wipes unclaimed emissions rewards without checking `emissions_outstanding` - ([File: programs/marginfi/src/state/marginfi_account.rs])

### Summary
`Balance::close()` unconditionally resets a position to `Balance::empty_deactivated()`, which zeroes `emissions_outstanding` — the user's already-accrued, unclaimed emissions rewards — without any check that this value is zero first. `withdraw_all`, `repay_all`, and `close_balance` (the only ways to fully close a `Balance` slot) all call `balance.close()` and none of them verify `emissions_outstanding == 0` before doing so, even though a dedicated error variant `CannotCloseOutstandingEmissions` exists in the codebase for exactly this purpose but is never invoked. This mirrors the reported bug class: state that legitimately holds unclaimed accrued value gets silently deleted/reset, permanently losing the payout for the user.

### Finding Description
Each `Balance` tracks `emissions_outstanding`, the unclaimed emissions reward accrued for that position [1](#0-0) .

`Balance::close()` resets the entire struct to `empty_deactivated()`, which hard-codes `emissions_outstanding` back to `I80F48::ZERO`, discarding whatever value was accrued: [2](#0-1)  and [3](#0-2) .

All three code paths that fully close a balance (the only way to remove a `Balance` slot per the integrator docs [4](#0-3) ) call `balance.close()` without first checking `emissions_outstanding`:
- `withdraw_all` only checks asset/liability amounts, never emissions, before calling `balance.close()` [5](#0-4) .
- `repay_all` similarly only checks liability/asset amounts [6](#0-5) .
- `close_balance` checks only that liability/asset amounts are zero before calling `balance.close()` [7](#0-6) .

Critically, the program already defines an error specifically for this scenario, `CannotCloseOutstandingEmissions` (error code 6033) — "Cannot close balance because of outstanding emissions" — but it is declared and never referenced/checked anywhere in the three closing paths [8](#0-7) . This strongly suggests the intended invariant (block closing until emissions are settled/claimed) is not actually enforced in the current closing logic, which is precisely the analog of the reported bug: a balance-zeroing action silently discards an accumulated, unclaimed payout because the code fails to first verify/settle the pending value before deactivating the position.

This is directly analogous to the external report's root cause: a user (or forwarder) zeroing out their balance triggers a deletion/reset path (`deleteUser`/`balance.close()`) that does not account for outstanding uncredited value (`calculatedPayout`/`emissions_outstanding`), silently erasing it. In both cases, the value is computed and stored on the position/account but the "close" cleanup path fails to check or flush it before wiping the record.

### Impact Explanation
A user (or an integrator managing balances programmatically) who withdraws/repays their full position or force-closes a dust balance while they have unclaimed `emissions_outstanding` permanently loses that reward — the funds/tokens were already earmarked for them (tracked on-chain in the `Balance`) but the record is deleted with no on-chain trace, and no instruction can recover it afterward since the balance slot is deactivated and its bytes zeroed. This is a permanent loss of legitimately owed rewards for the account holder, matching the "permanent lock/freeze" / loss-of-funds impact class.

### Likelihood Explanation
This can be triggered by any ordinary user action — `lending_account_withdraw` with `withdraw_all=true`, `lending_account_repay` with `repay=true`, or `lending_account_close_balance` — whenever a user has accrued emissions on a position and then fully closes it before claiming, which is a routine and easily reachable sequence (deposit → earn emissions → withdraw all without first claiming emissions). No special privileges or unusual preconditions are required.

### Recommendation
Before calling `balance.close()` in `withdraw_all`, `repay_all`, and `close_balance`, check that `balance.emissions_outstanding` is zero (or automatically settle it, e.g., by transferring/crediting it out first), returning `MarginfiError::CannotCloseOutstandingEmissions` if non-zero — analogous to the existing but unused error variant. Alternatively, auto-flush outstanding emissions to the account's `emissions_destination_account` as part of the close-balance flow instead of deleting them.

### Proof of Concept
Not runnable in this ask-only review; a suggested Rust test to add under `programs/marginfi/tests/user_actions/` would:
1. Deposit into a bank with an active emissions campaign so a `Balance.emissions_outstanding` accrues a non-zero value (see `emissions_same_bank_deposit_updates_asset_share_value` test setup for emissions bank configuration) [9](#0-8) .
2. Advance time/interest so emissions accrue on the balance without calling any emissions-claim instruction.
3. Call `lending_account_withdraw` with `withdraw_all = true` to fully close the position.
4. Assert that the transaction succeeds and that the `Balance` slot is deactivated (`active == 0`), with the previously non-zero `emissions_outstanding` now unrecoverable/lost, confirming no error such as `CannotCloseOutstandingEmissions` was raised and no settlement occurred.

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

**File:** programs/marginfi/src/state/marginfi_account.rs (L1743-1767)
```rust
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

**File:** guides/DEVELOPERS_INTEGRATORS/GETTING_STARTED_INTEGRATOR.md (L72-76)
```markdown
- `amount` is in native token, in native decimal, e.g. 1 SOL = 1 \* 10^9
- Set `withdraw_all` to "true" to ignore your amount input and withdraw the entire balance. This
is the only way to close a Balance so it no longer appears on your Account, simply withdrawing
by configuring `amount` will always leave the Balance on your account, even with zero shares.
</details>
```

**File:** programs/marginfi/src/errors.rs (L71-72)
```rust
    #[msg("Cannot close balance because of outstanding emissions")] // 6033
    CannotCloseOutstandingEmissions,
```

**File:** programs/marginfi/tests/misc/emissions_deposit.rs (L211-258)
```rust
#[tokio::test]
async fn emissions_same_bank_deposit_updates_asset_share_value() -> anyhow::Result<()> {
    let test_f = TestFixture::new(Some(TestSettings::all_banks_payer_not_admin())).await;

    let usdc_bank = test_f.get_bank(&BankMint::Usdc);

    let emissions_funding = test_f.usdc_mint.create_token_account_and_mint_to(50).await;

    let depositor_a = test_f.create_marginfi_account().await;
    let depositor_b = test_f.create_marginfi_account().await;

    let depositor_a_usdc = test_f.usdc_mint.create_token_account_and_mint_to(40).await;
    let depositor_b_usdc = test_f.usdc_mint.create_token_account_and_mint_to(60).await;

    let depositor_a_amount = 40;
    depositor_a
        .try_bank_deposit(
            depositor_a_usdc.key,
            usdc_bank,
            depositor_a_amount as f64,
            None,
        )
        .await?;

    let depositor_b_amount = 60;
    depositor_b
        .try_bank_deposit(
            depositor_b_usdc.key,
            usdc_bank,
            depositor_b_amount as f64,
            None,
        )
        .await?;

    let bank_before = usdc_bank.load().await;
    let shares_before = I80F48::from(bank_before.total_asset_shares);
    let share_value_before = I80F48::from(bank_before.asset_share_value);

    let liquidity_vault_before =
        TokenAccountFixture::fetch(test_f.context.clone(), bank_before.liquidity_vault)
            .await
            .balance()
            .await;

    let emissions_deposit = 50;
    usdc_bank
        .try_emissions_deposit(native!(emissions_deposit, "USDC"), emissions_funding.key)
        .await?;
```
