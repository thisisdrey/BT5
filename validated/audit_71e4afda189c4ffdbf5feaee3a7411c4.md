### Title
`lending_account_withdraw()` burns full share amount but pays out a clamped, smaller token amount when `TOKENLESS_REPAYMENTS_COMPLETE` is set - ([File: programs/marginfi/src/instructions/marginfi_account/withdraw.rs])

### Summary
In `lending_account_withdraw()`, when a bank has the `TOKENLESS_REPAYMENTS_COMPLETE` flag set, the actual token amount transferred to the withdrawer is clamped to `min(amount_pre_fee, actual_vault_balance)` *after* the share-burning accounting has already been applied using the un-clamped `amount_pre_fee`. This is the same root-cause pattern as the reported Turbo bug: an accounting update is performed with one amount, while the value that is actually moved/transferred is a different (smaller) amount computed later — producing a permanent state mismatch, here manifesting as an outright user loss rather than just a cap-tracking distortion.

### Finding Description
The non-`withdraw_all` branch computes `share_amount` by calling `bank_account.withdraw(I80F48::from_num(amount_pre_fee))` using the user-requested `amount_pre_fee`: [1](#0-0) 

This call to `BankAccountWrapper::withdraw` immediately mutates the account's `asset_shares` and the bank's `total_asset_shares` for the full requested amount, since `withdraw()` maps to `decrease_balance_internal(amount, WithdrawOnly)` which performs the share burn unconditionally (bounded only by the user's balance, not by the bank's actual liquidity): [2](#0-1) 

After that irreversible accounting change, `amount_pre_fee` is shadowed and reduced to `min(amount_pre_fee, actual)` when `TOKENLESS_REPAYMENTS_COMPLETE` is set, where `actual` is the bank liquidity vault's real token balance: [3](#0-2) 

This clamped, smaller `amount_pre_fee` is then used consistently for `record_withdrawal_outflow`, the `LendingAccountWithdrawEvent`, and — critically — `bank.withdraw_spl_transfer`, meaning the user physically receives less than the amount their shares were burned for: [4](#0-3) 

By contrast, the `withdraw_all` code path handles this correctly: `bank_account.withdraw_all(in_receivership)` derives the actual amount and burns exactly the matching share delta together, atomically, with no post-hoc re-clamp of shares vs. transferred amount: [5](#0-4) 

The `TOKENLESS_REPAYMENTS_COMPLETE` flag is set automatically once a bank's `TOKENLESS_REPAYMENTS_ALLOWED` sunset flow has zeroed out `total_liability_shares`, and this transition is triggered from an ordinary, unprivileged `lending_account_repay` call: [6](#0-5) 

Once that flag is set, any lender calling `lending_account_withdraw` with a specific `amount` (not `withdraw_all`) hits the vulnerable branch. This is exactly the class of bug in the referenced report: `master.onSafeLess(asset, vault, feiAmount)` in `TurboSafe.sol` used the clamped `feiDebt` for accounting after the vault already recorded the withdrawal of the original, larger `feiAmount`, desynchronizing tracked vs. actual state. Here the mismatch is worse: it is not merely an internal counter desync but a direct loss of user claim — shares representing a certain value are destroyed while a smaller value of tokens is actually transferred to the user, and the difference is not credited to anyone (it is not routed to `collected_insurance_fees_outstanding` or any other account, unlike the dust-handling in `withdraw_all`/`repay_all`).

### Impact Explanation
This causes real fund loss/insolvency exposure to an unprivileged lender withdrawing from a bank that has entered the "sunset"/tokenless-repayment state: their on-chain claim (asset shares) is reduced by the full requested amount, but they receive fewer tokens than that claim represented, with the excess vanishing rather than being redistributed or tracked. This can also distort protocol-wide accounting since the burned shares (and thus `bank.total_asset_shares`) no longer correspond to the sum of tokens actually paid out, silently changing the implied share value for any remaining depositors in a way that is not tracked as insurance/fee dust. This matches the "unauthorized state change" / "insolvency and unaccounted loss" impact bar.

### Likelihood Explanation
Reachable only after a risk admin has begun the sunset/deleverage flow for a specific bank and set `TOKENLESS_REPAYMENTS_ALLOWED`, and only once repayments have driven `total_liability_shares` to ~zero (setting `TOKENLESS_REPAYMENTS_COMPLETE`). This is an intended, documented feature path (see `guides/ADMIN/BANK_STATE.md` and `tests/specs/bankruptcy/zb02_e2eSunset.spec.ts`) rather than a purely theoretical state, and the withdraw call itself is made by an ordinary, unprivileged lender — not a privileged actor. Likelihood is therefore moderate: it requires the bank to already be in this specific insolvency/sunset condition, but once there, any lender doing a partial (non-`withdrawAll`) withdraw triggers the bug automatically, with no special crafting needed.

### Recommendation
Compute the actual transferable amount (`min(requested amount_pre_fee, vault balance)`) *before* calling `bank_account.withdraw()`, and burn shares only for that clamped amount — mirroring the atomic design already used in `withdraw_all`/`repay_all`. If a shortfall must be tracked, route the difference into `bank.collected_insurance_fees_outstanding` (or equivalent) exactly as `withdraw_all` does with fractional dust, so no value silently disappears from the bank's accounting.

### Proof of Concept
1. Risk admin drives a bank into deleverage/sunset mode; through the tokenless-repayment flow (`lending_account_repay` with `repay_all=true`, risk admin authority, `TOKENLESS_REPAYMENTS_ALLOWED` set) the bank's `total_liability_shares` reaches ~0 and `bank.update_flag(true, TOKENLESS_REPAYMENTS_COMPLETE)` fires (`repay.rs:142-149`).
2. The bank's liquidity vault now holds less in tokens than the sum of lenders' claims (the underlying scenario this feature exists for, per `guides/ADMIN/BANK_STATE.md` and `zb02_e2eSunset.spec.ts`).
3. An unprivileged lender with an active asset balance calls `lending_account_withdraw(amount, withdraw_all=false)` requesting `amount_pre_fee` greater than the vault's remaining `actual` balance but within their own share-derived balance.
4. `bank_account.withdraw(amount_pre_fee)` (line 128) burns shares for the full `amount_pre_fee` from the user's balance and the bank's `total_asset_shares`.
5. The subsequent clamp (`withdraw.rs:134-144`) reduces the transferred amount to `actual` (less than `amount_pre_fee`).
6. `bank.withdraw_spl_transfer(amount_pre_fee /* now = actual */, ...)` sends only `actual` tokens to the user, while their shares (and the bank's total) were already reduced for the larger, originally requested amount — the difference in value is permanently lost/unaccounted-for, unlike the analogous dust-handling logic present in `withdraw_all`/`repay_all`.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L115-131)
```rust
        } else {
            let amount_pre_fee = maybe_bank_mint
                .as_ref()
                .map(|mint| {
                    utils::calculate_pre_fee_spl_deposit_amount(
                        mint.to_account_info(),
                        amount,
                        clock.epoch,
                    )
                })
                .transpose()?
                .unwrap_or(amount);

            let share_amount = bank_account.withdraw(I80F48::from_num(amount_pre_fee))?;

            (amount_pre_fee, share_amount)
        };
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L133-144)
```rust
        // If in deleverage mode and deleverage is complete, you get what's left!
        let amount_pre_fee = if bank.get_flag(TOKENLESS_REPAYMENTS_COMPLETE) {
            let actual = accessor::amount(&bank_liquidity_vault.to_account_info())?;
            msg!(
                "amount expected withdrawn: {:?}, actual: {:?}",
                amount_pre_fee,
                actual
            );
            u64::min(amount_pre_fee, actual)
        } else {
            amount_pre_fee
        };
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L146-191)
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

        marginfi_account.last_update = clock.unix_timestamp as u64;

        bank.withdraw_spl_transfer(
            amount_pre_fee,
            bank_liquidity_vault.to_account_info(),
            destination_token_account.to_account_info(),
            bank_liquidity_vault_authority.to_account_info(),
            maybe_bank_mint.as_ref(),
            token_program.to_account_info(),
            bank_signer!(
                BankVaultType::Liquidity,
                bank_loader.key(),
                liquidity_vault_authority_bump
            ),
            ctx.remaining_accounts,
        )?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1597-1601)
```rust
    /// Withdraw an asset, will error if there is not enough asset - borrowing is not allowed.
    /// Returns the asset share delta burned.
    pub fn withdraw(&mut self, amount: I80F48) -> MarginfiResult<I80F48> {
        self.decrease_balance_internal(amount, BalanceDecreaseType::WithdrawOnly)
    }
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1623-1679)
```rust
    /// Withdraw existing asset in full - will error if there is no asset.
    /// When `in_receivership` is true, clears the bank's liquidation price cache lock
    /// so that banks whose balances are closed mid-liquidation don't stay permanently locked.
    /// Returns `(spl_withdraw_amount, asset_share_delta)`.
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

        // Only clear the lock when this account is actually in receivership.
        // The lock is bank-level global state, so clearing it unconditionally
        // would affect unrelated accounts sharing the same bank.
        if in_receivership {
            bank.cache.clear_liquidation_price_cache_locked();
        }

        bank.decrement_lending_position_count();
        bank.change_asset_shares(-total_asset_shares, false)?;
        bank.check_utilization_ratio()?;

        let spl_withdraw_amount = current_asset_amount
            .checked_floor()
            .ok_or_else(math_error!())?;

        bank.collected_insurance_fees_outstanding = {
            current_asset_amount
                .checked_sub(spl_withdraw_amount)
                .ok_or_else(math_error!())?
                .checked_add(bank.collected_insurance_fees_outstanding.into())
                .ok_or_else(math_error!())?
                .into()
        };

        let spl_withdraw_amount = spl_withdraw_amount
            .checked_to_num()
            .ok_or_else(math_error!())?;

        Ok((spl_withdraw_amount, total_asset_shares))
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/repay.rs (L142-149)
```rust
    // During deleverage, once the last repayment is complete, and the bank's debts have been fully
    // discharged, the risk admin becomes empowered to purge the balances of lenders
    let liabs: I80F48 = bank.total_liability_shares.into();
    if bank.get_flag(TOKENLESS_REPAYMENTS_ALLOWED)
        && liabs.abs() < ZERO_AMOUNT_THRESHOLD * I80F48!(10)
    {
        bank.update_flag(true, TOKENLESS_REPAYMENTS_COMPLETE);
    }
```
