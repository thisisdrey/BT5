Confirmed: `LendingPoolCollectBankFees` (permissionless, unprivileged, no signer check) is a strong analog for the reported bug class.

### Title
Permanent DoS of bank fee collection for tokens that revert on zero-value transfers - (File: `programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs`)

### Summary
`lending_pool_collect_bank_fees` unconditionally calls `bank.withdraw_spl_transfer(...)` for the group fee, insurance fee, and program fee legs, even when the computed transfer amount is `0`. `withdraw_spl_transfer` performs a raw SPL transfer with no zero-amount guard [1](#0-0) . If any of the three fee amounts is `0` and the bank's mint reverts on zero-value transfers, the entire instruction reverts, permanently blocking fee harvesting for that bank.

### Finding Description
`lending_pool_collect_bank_fees` computes three fee legs (`group_fee_transfer_amount`, `insurance_fee_transfer_amount`, `program_fee_transfer_amount`) as `min(outstanding, available_liquidity)`, and any of these can legitimately be `0` — e.g., if only insurance fees have accrued but group/program fees have not yet accrued for that bank. The function then calls `bank.withdraw_spl_transfer` unconditionally for each leg, without checking for a zero amount: [2](#0-1) 

`withdraw_spl_transfer` performs a direct CPI `transfer`/`transfer_checked` call with the given `amount`, with no `if amount != 0` guard, unlike other paths in the codebase (e.g. `claim_bad_debt.rs`'s `cpi_transfer_to_destination`, which explicitly early-returns on `amount == 0` [3](#0-2) ): [4](#0-3) 

This is the exact bug class from the report: a zero-value SPL transfer is issued unconditionally as part of a normal, expected code path (removing/collecting past accumulated amounts), which reverts for tokens implementing a zero-transfer guard (a known pattern among certain SPL Token-2022 mints and custom tokens).

The same unconditional-transfer pattern also exists in `lending_pool_withdraw_fees_permissionless` (permissionless, amount clamped to `min(requested, fee_vault.amount)`, which can be `0` if the fee vault is empty) [5](#0-4) , and in `handle_bankruptcy.rs`'s insurance-coverage transfer [6](#0-5) , but `lending_pool_collect_bank_fees` is the strongest analog since it is the routine, expected, permissionless path that always attempts three separate transfers and is virtually guaranteed to have at least one zero-amount leg under normal operating conditions (banks rarely accrue all three fee types simultaneously in the same call window).

### Impact Explanation
For any bank whose mint reverts on zero-amount transfers, `lending_pool_collect_bank_fees` becomes permanently uncallable, since it always attempts three transfers, and it is highly likely that at least one of the fee legs (group, insurance, or program fee) is zero at any given time. This permanently locks/prevents fee collection for that bank — a protocol-level fund-flow lock, not merely an inconvenience, since group, insurance, and program fees can never be swept out of the liquidity vault into their respective vaults for that bank. This matches the "permanent lock/freeze" impact category.

### Likelihood Explanation
Likelihood is high given normal operation: fees accrue asynchronously (insurance fees, group fees, and program fees typically accrue at different rates/schedules per bank), so it is common for the collect-fees instruction to be called while one or more of the three legs is legitimately zero. The instruction is permissionless and callable by anyone/any keeper, so it will inevitably be invoked in a state with at least one zero leg, immediately triggering the revert for a susceptible mint. No admin privilege or attacker action is needed to trigger this — it is a passive DoS activated by normal usage combined with the choice of an incompatible mint.

### Recommendation
Guard each `withdraw_spl_transfer` call (and the underlying `deposit_spl_transfer`/`withdraw_spl_transfer` helpers in `bank.rs`) with an `if amount != 0` check before issuing the CPI transfer, mirroring the pattern already used in `claim_bad_debt.rs`'s `cpi_transfer_to_destination`. Apply this fix uniformly in `collect_bank_fees.rs` for all three fee legs, in `lending_pool_withdraw_fees_permissionless`, and in `handle_bankruptcy.rs`'s insurance-coverage transfer.

### Proof of Concept
1. Create a bank whose mint enforces a zero-value-transfer revert (e.g., a Token-2022 mint with a transfer hook that rejects `amount == 0`, or any SPL-compatible token with such logic).
2. Let the bank accrue only insurance fees (e.g., via liquidation) while group/program fees remain at `0` (their default/initial state, or after being fully collected previously).
3. Call `lending_pool_collect_bank_fees` for this bank.
4. Observe: the `group_fee_transfer_amount` (or `program_fee_transfer_amount`) computed leg is `0`; `bank.withdraw_spl_transfer(0, ...)` is invoked unconditionally, triggering the mint's zero-transfer revert, and the entire instruction reverts — permanently blocking fee collection for this bank until the state changes (which may never happen, e.g., if group fee rate is configured such that it never accrues for that bank).

### Citations

**File:** programs/marginfi/src/state/bank.rs (L769-814)
```rust
    fn withdraw_spl_transfer<'info>(
        &self,
        amount: u64,
        from: AccountInfo<'info>,
        to: AccountInfo<'info>,
        authority: AccountInfo<'info>,
        maybe_mint: Option<&InterfaceAccount<'info, Mint>>,
        program: AccountInfo<'info>,
        signer_seeds: &[&[&[u8]]],
        remaining_accounts: &[AccountInfo<'info>],
    ) -> MarginfiResult {
        debug!(
            "withdraw_spl_transfer: amount: {} from {} to {}, auth {}",
            amount, from.key, to.key, authority.key
        );

        #[cfg(feature = "client")]
        if let Some(mint) = maybe_mint {
            invoke_client_token_transfer(
                program.key,
                amount,
                from,
                Some(mint.to_account_info()),
                to,
                authority,
                Some(mint.decimals),
                remaining_accounts,
                signer_seeds,
            )?;
        } else {
            // `transfer_checked` and `transfer` does the same thing, the additional `_checked` logic
            // is only to assert the expected attributes by the user (mint, decimal scaling),
            //
            // Security of `transfer` is equal to `transfer_checked`.
            invoke_client_token_transfer(
                program.key,
                amount,
                from,
                None,
                to,
                authority,
                None,
                remaining_accounts,
                signer_seeds,
            )?;
        }
```

**File:** programs/marginfi/src/state/bank.rs (L816-847)
```rust
        #[cfg(not(feature = "client"))]
        if let Some(mint) = maybe_mint {
            spl_token_2022::onchain::invoke_transfer_checked(
                program.key,
                from,
                mint.to_account_info(),
                to,
                authority,
                remaining_accounts,
                amount,
                mint.decimals,
                signer_seeds,
            )?;
        } else {
            // `transfer_checked` and `transfer` does the same thing, the additional `_checked` logic
            // is only to assert the expected attributes by the user (mint, decimal scaling),
            //
            // Security of `transfer` is equal to `transfer_checked`.
            #[allow(deprecated)]
            transfer(
                CpiContext::new_with_signer(
                    program.key(),
                    Transfer {
                        from,
                        to,
                        authority,
                    },
                    signer_seeds,
                ),
                amount,
            )?;
        }
```

**File:** programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs (L93-125)
```rust
    bank.withdraw_spl_transfer(
        group_fee_transfer_amount
            .checked_to_num()
            .ok_or_else(math_error!())?,
        liquidity_vault.to_account_info(),
        fee_vault.to_account_info(),
        liquidity_vault_authority.to_account_info(),
        maybe_bank_mint.as_ref(),
        token_program.to_account_info(),
        bank_signer!(
            BankVaultType::Liquidity,
            ctx.accounts.bank.key(),
            bank.liquidity_vault_authority_bump
        ),
        ctx.remaining_accounts,
    )?;

    bank.withdraw_spl_transfer(
        insurance_fee_transfer_amount
            .checked_to_num()
            .ok_or_else(math_error!())?,
        liquidity_vault.to_account_info(),
        insurance_vault.to_account_info(),
        liquidity_vault_authority.to_account_info(),
        maybe_bank_mint.as_ref(),
        token_program.to_account_info(),
        bank_signer!(
            BankVaultType::Liquidity,
            ctx.accounts.bank.key(),
            bank.liquidity_vault_authority_bump
        ),
        ctx.remaining_accounts,
    )?;
```

**File:** programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs (L451-486)
```rust
pub fn lending_pool_withdraw_fees_permissionless<'info>(
    mut ctx: Context<'info, LendingPoolWithdrawFeesPermissionless<'info>>,
    amount: u64,
) -> MarginfiResult {
    let LendingPoolWithdrawFeesPermissionless {
        bank: bank_loader,
        fee_vault,
        fee_vault_authority,
        fees_destination_account,
        token_program,
        ..
    } = ctx.accounts;

    let bank = bank_loader.load()?;

    // Withdraw all if there aren't enough funds to facilitate the withdraw as requested.
    let amount = u64::min(amount, fee_vault.amount);
    let fees_token_program = &token_program.key();

    let maybe_bank_mint =
        utils::maybe_take_bank_mint(&mut ctx.remaining_accounts, &bank, fees_token_program)?;

    bank.withdraw_spl_transfer(
        amount,
        fee_vault.to_account_info(),
        fees_destination_account.to_account_info(),
        fee_vault_authority.to_account_info(),
        maybe_bank_mint.as_ref(),
        token_program.to_account_info(),
        bank_signer!(
            BankVaultType::Fee,
            bank_loader.key(),
            bank.fee_vault_authority_bump
        ),
        ctx.remaining_accounts,
    )?;
```

**File:** programs/marginfi/src/instructions/drift/claim_bad_debt.rs (L253-257)
```rust
    fn cpi_transfer_to_destination(&self) -> MarginfiResult<u64> {
        let amount = accessor::amount(&self.claimant_token_account.to_account_info())?;
        if amount == 0 {
            return Ok(0);
        }
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L174-187)
```rust
    bank.withdraw_spl_transfer(
        insurance_coverage_deposit_pre_fee,
        ctx.accounts.insurance_vault.to_account_info(),
        ctx.accounts.liquidity_vault.to_account_info(),
        ctx.accounts.insurance_vault_authority.to_account_info(),
        maybe_bank_mint.as_ref(),
        token_program.to_account_info(),
        bank_signer!(
            BankVaultType::Insurance,
            bank_loader.key(),
            bank.insurance_vault_authority_bump
        ),
        ctx.remaining_accounts,
    )?;
```
