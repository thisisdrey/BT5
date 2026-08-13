Based on my research, I found a partial analog to this bug class in marginfi-v2's integration deposit paths (Kamino, Solend, JupLend, Drift), which is architecturally distinct from the already-mitigated core deposit path.

### Title
Integration deposit paths (Kamino/Solend/JupLend/Drift) do not account for transfer fees, causing deposit failure/DoS on fee-bearing mints - (File: `programs/marginfi/src/instructions/juplend/deposit.rs`, `programs/marginfi/src/instructions/kamino/init_obligation.rs`, `programs/marginfi/src/instructions/solend/deposit.rs`)

### Summary
The core `lending_account_deposit` instruction correctly compensates for Token-2022 transfer fees by computing `amount_pre_fee` via `utils::calculate_pre_fee_spl_deposit_amount` before calling `bank.deposit_spl_transfer` [1](#0-0) . However, the newer external-integration deposit flows (JupLend, Kamino, Solend, Drift) transfer the raw, un-adjusted `amount` from the user to the bank's vault via `transfer_checked`, and then immediately re-use that same nominal `amount` in the follow-up CPI call to the external lending protocol.

### Finding Description
In `juplend_deposit`, the code computes `expected_shares` from the nominal `amount` using `expected_shares_for_deposit_from_rates`, then calls `ctx.accounts.cpi_transfer_user_to_liquidity_vault(amount)` followed immediately by `ctx.accounts.cpi_juplend_deposit(amount, authority_bump)` [2](#0-1) . If the mint carries a Token-2022 transfer fee, the liquidity vault only receives `amount - fee` tokens, but the subsequent `cpi_juplend_deposit` call attempts to move the full `amount` out of that same vault into the external JupLend reserve — a transfer that will fail due to insufficient vault balance.

The same pattern appears in `kamino_init_obligation`, which calls `cpi_transfer_user_to_obligation_owner(amount)` followed by `cpi_kamino_deposit(amount)` using the identical nominal `amount` [3](#0-2) , and in `SolendDeposit::cpi_transfer_user_to_liquidity_vault`/`cpi_solend_deposit`, which also transfer and then re-deposit the same `amount` [4](#0-3) . None of these integration paths perform the balance-before/balance-after reconciliation or the `calculate_pre_fee_spl_deposit_amount`/`calculate_post_fee_spl_deposit_amount` adjustment that the core deposit path uses (these helpers exist in `programs/marginfi/src/utils/general.rs` but are only invoked from `lending_account_deposit`) [5](#0-4) .

### Impact Explanation
On a fee-bearing Token-2022 mint used for one of these integration banks, deposits would revert due to insufficient vault balance for the second-leg CPI transfer, rather than silently succeeding with a wrong accounting. This is a denial-of-service on deposits for that bank/mint combination rather than direct fund loss, since the transaction fails atomically (the whole instruction reverts, so no partial state corruption or share/asset mismatch is committed on-chain). This differs from the original report's exact impact (funds get "stuck mid-flow" causing revert) but is the closest reachable analog in unprivileged user-facing deposit flows.

### Likelihood Explanation
Likelihood depends entirely on whether Kamino/Solend/JupLend/Drift integration banks can actually be configured with Token-2022 fee-bearing mints. My review found that these instruction files import `anchor_spl::token_interface::{transfer_checked, TransferChecked}` generically (not restricted to legacy `Token`), suggesting Token-2022 mints are architecturally supported. I was unable to fully verify, within the available context, whether `validate_asset_tags` or the bank-init flow for these specific asset tags (`ASSET_TAG_KAMINO`, `ASSET_TAG_SOLEND`, `ASSET_TAG_JUPLEND`, `ASSET_TAG_DRIFT`) affirmatively blocks transfer-fee mints from being onboarded. If such a mint-level restriction exists and is enforced at bank-add time, this issue would not be reachable in practice.

### Recommendation
For each integration deposit path (JupLend, Kamino, Solend, Drift), record the vault's token balance before and after the initial `transfer_checked` from the user, and use the actual received delta (post-fee amount) for both the expected-shares calculation and the subsequent CPI deposit call into the external protocol, mirroring the `calculate_pre_fee_spl_deposit_amount` pattern already used in `lending_account_deposit`.

### Proof of Concept
1. Group admin creates a JupLend/Kamino/Solend integration bank backed by a Token-2022 mint that has an active `TransferFeeConfig` extension.
2. A user calls the corresponding deposit instruction (e.g., `juplend_deposit`) with `amount = 100_000`.
3. `cpi_transfer_user_to_liquidity_vault(amount)` moves 100_000 nominal tokens, but the vault only receives `100_000 - fee` due to the transfer fee extension.
4. The subsequent `cpi_juplend_deposit(amount, ...)` (or `cpi_kamino_deposit`/`cpi_solend_deposit`) attempts to transfer the full `100_000` out of the vault into the external protocol's reserve, which fails because the vault's actual balance is `100_000 - fee`.
5. The instruction reverts, and the user cannot deposit that mint into this integration bank type.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L104-124)
```rust
    let amount_pre_fee = maybe_bank_mint
        .as_ref()
        .map(|mint| {
            utils::calculate_pre_fee_spl_deposit_amount(
                mint.to_account_info(),
                deposit_amount,
                clock.epoch,
            )
        })
        .transpose()?
        .unwrap_or(deposit_amount);

    bank.deposit_spl_transfer(
        amount_pre_fee,
        signer_token_account.to_account_info(),
        bank_liquidity_vault.to_account_info(),
        signer.to_account_info(),
        maybe_bank_mint.as_ref(),
        token_program.to_account_info(),
        ctx.remaining_accounts,
    )?;
```

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L55-70)
```rust
    let expected_shares = {
        let lending = ctx.accounts.integration_acc_1.load()?;
        // Compute expected shares minted (round-down) using the same math as JupLend.
        expected_shares_for_deposit_from_rates(
            amount,
            lending.liquidity_exchange_price,
            lending.token_exchange_price,
        )
        .ok_or_else(|| error!(MarginfiError::MathError))?
    };

    let pre_f_token_balance = accessor::amount(&ctx.accounts.integration_acc_2.to_account_info())?;

    // Move underlying into the vault and deposit into JupLend.
    ctx.accounts.cpi_transfer_user_to_liquidity_vault(amount)?;
    ctx.accounts.cpi_juplend_deposit(amount, authority_bump)?;
```

**File:** programs/marginfi/src/instructions/kamino/init_obligation.rs (L42-45)
```rust
    // Transfer tokens from user (signer_token_account) -> obligation owner (liquidity vault)
    ctx.accounts.cpi_transfer_user_to_obligation_owner(amount)?;
    // Deposit into Kamino (liquidity vault) -> (reserve_liquidity_supply)
    ctx.accounts.cpi_kamino_deposit(amount)?;
```

**File:** programs/marginfi/src/instructions/solend/deposit.rs (L261-300)
```rust
    pub fn cpi_transfer_user_to_liquidity_vault(&self, amount: u64) -> MarginfiResult {
        let program = self.token_program.to_account_info();
        let accounts = TransferChecked {
            from: self.signer_token_account.to_account_info(),
            to: self.liquidity_vault.to_account_info(),
            authority: self.authority.to_account_info(),
            mint: self.mint.to_account_info(),
        };
        let cpi_ctx = CpiContext::new(program.key(), accounts);
        let decimals = self.mint.decimals;
        transfer_checked(cpi_ctx, amount, decimals)?;
        Ok(())
    }

    pub fn cpi_solend_deposit(&self, amount: u64, authority_bump: u8) -> MarginfiResult {
        let accounts = DepositReserveLiquidityAndObligationCollateral {
            source_liquidity_info: self.liquidity_vault.to_account_info(),
            user_collateral_info: self.user_collateral.to_account_info(),
            reserve_info: self.integration_acc_1.to_account_info(),
            reserve_liquidity_supply_info: self.reserve_liquidity_supply.to_account_info(),
            reserve_collateral_mint_info: self.reserve_collateral_mint.to_account_info(),
            lending_market_info: self.lending_market.to_account_info(),
            lending_market_authority_info: self.lending_market_authority.to_account_info(),
            destination_deposit_collateral_info: self.reserve_collateral_supply.to_account_info(),
            obligation_info: self.integration_acc_2.to_account_info(),
            obligation_owner_info: self.liquidity_vault_authority.to_account_info(),
            pyth_price_info: self.pyth_price.to_account_info(),
            switchboard_feed_info: self.switchboard_feed.to_account_info(),
            user_transfer_authority_info: self.liquidity_vault_authority.to_account_info(),
            token_program_info: self.token_program.to_account_info(),
        };
        let signer_seeds: &[&[&[u8]]] =
            bank_signer!(BankVaultType::Liquidity, self.bank.key(), authority_bump);

        // Create CPI context with signer
        let cpi_ctx =
            CpiContext::new_with_signer(self.solend_program.key(), accounts, signer_seeds);
        deposit_reserve_liquidity_and_obligation_collateral(cpi_ctx, amount)?;
        Ok(())
    }
```

**File:** programs/marginfi/src/utils/general.rs (L87-112)
```rust
pub fn calculate_post_fee_spl_deposit_amount(
    mint_ai: AccountInfo,
    input_amount: u64,
    epoch: u64,
) -> MarginfiResult<u64> {
    if mint_ai.owner.eq(&Token::id()) {
        return Ok(input_amount);
    }

    let mint_data = mint_ai.try_borrow_data()?;
    let mint = StateWithExtensions::<spl_token_2022::state::Mint>::unpack(&mint_data)?;

    let fee = if let Ok(transfer_fee_config) = mint.get_extension::<TransferFeeConfig>() {
        transfer_fee_config
            .calculate_epoch_fee(epoch, input_amount)
            .unwrap()
    } else {
        0
    };

    let output_amount = input_amount
        .checked_sub(fee)
        .ok_or(MarginfiError::MathError)?;

    Ok(output_amount)
}
```
