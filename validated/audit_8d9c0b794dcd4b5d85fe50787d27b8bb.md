Based on my research, I found a valid structural analog in the marginfi-v2 codebase's fee collection path.

### Title
Permanent lock of bank fees via revert-on-zero-transfer Token-2022 mints in `lending_pool_collect_bank_fees` - (File: `programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs`)

### Summary
The Axis Finance bug (H-2) shows that unconditionally calling a token transfer with a value that can legitimately equal zero causes a permanent lock of funds when the underlying token reverts on zero-amount transfers. marginfi-v2 has an analogous pattern in `lending_pool_collect_bank_fees`, which unconditionally issues three separate SPL transfers (insurance, group, program fees) without checking that each individual amount is greater than zero, and the program's own token-transfer helper (`withdraw_spl_transfer`) never skips zero-value transfers.

### Finding Description
`lending_pool_collect_bank_fees` computes three transfer amounts and calls `bank.withdraw_spl_transfer` for each one unconditionally: [1](#0-0) [2](#0-1) 

`withdraw_spl_transfer` (and `deposit_spl_transfer`) never special-case an amount of `0` — they always invoke `transfer_checked`/`transfer`, regardless of the numeric amount: [3](#0-2) 

marginfi supports Token-2022 mints, including mints with an active `TransferHook` extension, as verified by `has_transfer_hook`: [4](#0-3) 

Only `lending_pool_emissions_deposit` explicitly rejects mints with an active transfer hook or nonzero transfer fee: [5](#0-4) 

No equivalent check exists for `lending_pool_collect_bank_fees`, `lending_pool_withdraw_fees`, or `lending_pool_withdraw_insurance`. If a bank's mint is a Token-2022 mint with an active transfer hook program that reverts on a zero-amount transfer call (a legitimate, spec-compliant hook behavior), then whenever one of the three fee legs computes to exactly `0` (which the docs confirm is the **historical default** for the program/origination fee: "Origination fee - Historically, always zero" / program fee accrues only via specific conditions), the whole `lending_pool_collect_bank_fees` instruction will revert on that leg's transfer. [6](#0-5) 

Because this instruction is permissionless and is the sole mechanism to move `collected_insurance_fees_outstanding` / `collected_group_fees_outstanding` from `liquidity_vault` into `insurance_vault`/`fee_vault` (from which `lending_pool_withdraw_fees`/`lending_pool_withdraw_insurance` can move funds out), a persistently-zero leg (e.g., program fee, which requires no borrows accruing that specific portion) makes every future call to `lending_pool_collect_bank_fees` revert, permanently preventing collection of the other non-zero fee legs as well, since all three transfers happen inside one instruction/transaction.

### Impact Explanation
This would permanently lock accrued insurance and group fees inside the bank's `liquidity_vault` for any bank backed by a Token-2022 mint with a transfer hook that reverts on zero transfers, since the fee-collection instruction can never succeed once one of its three legs is stuck at zero. This matches the "permanent lock/freeze of funds" impact category validated by the report.

### Likelihood Explanation
Likelihood is conditioned on the group admin/token issuer choosing to list a bank backed by a Token-2022 mint whose transfer-hook program reverts on zero-value transfers — this is a plausible, permitted configuration since marginfi explicitly supports Token-2022 with active transfer hooks for ordinary deposit/withdraw/fee flows (only `emissions_deposit` blocks such hooks). No admin/privileged action by the attacker is required to trigger the revert; it happens automatically whenever a legitimate permissionless caller invokes fee collection while one leg is zero, which is common given fee legs like the program fee are usually zero.

### Recommendation
In `lending_pool_collect_bank_fees` (and any other unconditional withdraw/deposit transfer call), skip the SPL transfer call entirely when the computed amount is `0`, matching the pattern already used in `lending_pool_emissions_deposit` and `DriftClaimBadDebt::cpi_transfer_to_destination` (`if amount == 0 { return Ok(0); }`): [7](#0-6) 

### Proof of Concept
1. Group admin adds a bank backed by a Token-2022 mint with an active `TransferHook` extension whose hook program reverts on a `0`-amount `Execute` instruction (a compliant Token-2022 hook behavior, and this mint configuration is not rejected at bank-add time, unlike `lending_pool_emissions_deposit`).
2. Interest accrues normally so `collected_insurance_fees_outstanding` and `collected_group_fees_outstanding` become non-zero, while `collected_program_fees_outstanding` remains `0` (its historical default, per `guides/ADMIN/COLLECTING_FEES.md`).
3. Anyone calls `lending_pool_collect_bank_fees`. The instruction reaches the third `withdraw_spl_transfer` call for the program fee with `amount = 0`, which invokes the transfer-hook program via `transfer_checked`, and the hook reverts.
4. The whole transaction reverts, so `insurance_fees_outstanding` and `group_fees_outstanding` (which had computed non-zero transfer amounts) are never moved out of `liquidity_vault`. Because the program fee leg will remain `0` on every retry, this failure recurs indefinitely, permanently blocking collection of both other fees for that bank.

### Citations

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

**File:** programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs (L146-163)
```rust
    bank.collected_program_fees_outstanding = new_outstanding_program_fees.into();

    bank.withdraw_spl_transfer(
        program_fee_transfer_amount
            .checked_to_num()
            .ok_or_else(math_error!())?,
        liquidity_vault.to_account_info(),
        fee_ata.to_account_info(),
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

**File:** programs/marginfi/src/state/bank.rs (L768-850)
```rust
    #[allow(unused_variables)]
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

        Ok(())
    }
```

**File:** programs/marginfi/src/utils/general.rs (L133-149)
```rust
/// Returns `true` if the given mint has an active transfer hook program.
/// If the hook is present but no program is active it would return false.
pub fn has_transfer_hook(mint_ai: AccountInfo) -> MarginfiResult<bool> {
    if mint_ai.owner.eq(&Token::id()) {
        return Ok(false);
    }

    let mint_data = mint_ai.try_borrow_data()?;
    let mint = StateWithExtensions::<spl_token_2022::state::Mint>::unpack(&mint_data)?;

    if let Ok(hook) = mint.get_extension::<TransferHook>() {
        let program_id: Option<Pubkey> = Option::from(hook.program_id);
        return Ok(program_id.is_some());
    }

    Ok(false)
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L100-109)
```rust
    // Reject mints with non-zero transfer fees or active transfer hooks.
    let mint_ai = ctx.accounts.mint.to_account_info();
    check!(
        !utils::nonzero_fee(mint_ai.clone(), clock.epoch)?,
        MarginfiError::InvalidTransfer
    );
    check!(
        !utils::has_transfer_hook(mint_ai)?,
        MarginfiError::InvalidTransfer
    );
```

**File:** guides/ADMIN/COLLECTING_FEES.md (L35-41)
```markdown
- **Origination fee** - Historically, always zero. The group admin configures this as a percentage
  of the borrow, and the program admin determine what portion of this fee goes to them. For example,
  if the origination fee is 1%, and the program_ir is 10%, then the group gets 0.9% and the program
  gets 0.1% of any borrow. A user borrowing \$100 would pay \$101, where 90 cents goes to the group
  and 10 cents to the program.
- **Bank Init flat sol fee** - Historically, always zero. Automatically debited to the global
  program fee wallet any time a bank is created.
```

**File:** programs/marginfi/src/instructions/drift/claim_bad_debt.rs (L253-257)
```rust
    fn cpi_transfer_to_destination(&self) -> MarginfiResult<u64> {
        let amount = accessor::amount(&self.claimant_token_account.to_account_info())?;
        if amount == 0 {
            return Ok(0);
        }
```
