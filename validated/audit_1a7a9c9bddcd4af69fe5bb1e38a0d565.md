## Analysis

marginfi-v2 integrates with external lending protocols (Kamino, Drift) via CPI-based deposit instructions. Both `kamino_deposit` and `drift_deposit` forward the user-specified `amount` directly into an external protocol's CPI call without checking that protocol's own deposit-capacity limits beforehand, unlike the native `lending_account_deposit` path, which explicitly supports `deposit_up_to_limit` to gracefully cap the amount and avoid reverting.

### Title
Missing pre-check of external protocol deposit caps in `kamino_deposit`/`drift_deposit` causes DoS on integration deposits - ([File: programs/marginfi/src/instructions/kamino/deposit.rs], [File: programs/marginfi/src/instructions/drift/deposit.rs])

### Summary
`kamino_deposit` and `drift_deposit` both immediately CPI into the external protocol's deposit instruction with the raw user-supplied `amount`, without first checking whether that deposit would exceed the external protocol's own deposit cap (Kamino reserve `deposit_withdrawal_cap`, Drift spot market `max_token_deposits`). This mirrors the Napier `RenzoAdapter._stake` bug class: staking/depositing into an external protocol without checking its stake/deposit limit beforehand, so the whole transaction reverts once the external limit is reached.

### Finding Description
In `kamino_deposit`, the instruction calls `ctx.accounts.cpi_kamino_deposit(amount, authority_bump)` directly after transferring funds, with no prior check of the Kamino reserve's remaining deposit capacity: [1](#0-0) 

The CPI call itself is: [2](#0-1) 

Similarly, `drift_deposit` forwards `amount` straight into `cpi_drift_deposit` with no check against the Drift spot market's `max_token_deposits`: [3](#0-2) 

By contrast, the native (non-integration) deposit path explicitly implements a defensive cap mechanism to avoid reverting when marginfi's *own* bank-level `deposit_limit` is close to being exhausted, via `deposit_up_to_limit`: [4](#0-3) 

No equivalent capping mechanism exists for the Kamino/Drift integration paths against the *external* protocol's cap — `kamino_deposit`/`drift_deposit` take no `deposit_up_to_limit`-like flag and perform no query of the reserve's/spot market's own limit before issuing the CPI.

### Impact Explanation
When the underlying Kamino reserve or Drift spot market approaches or is at its own protocol-enforced deposit cap, any `kamino_deposit`/`drift_deposit` call for an amount that would push the external protocol over its cap will simply revert inside the external program's CPI, aborting the whole marginfi transaction. This denies users the ability to deposit into that Kamino/Drift-backed bank once the external cap is close to being hit, exactly as in the Renzo `depositETH` `MaxTVLReached` scenario. This is a denial-of-service on the deposit path for that specific integration bank; it does not, by itself, cause fund loss or bad debt.

### Likelihood Explanation
This requires no privileged action — any unprivileged user attempting `kamino_deposit` or `drift_deposit` on a bank whose backing Kamino reserve/Drift market has a configured deposit cap (a normal external-protocol configuration) can trigger the revert simply by depositing an amount that would exceed the remaining external capacity, which is a realistic and externally-controlled condition (caps are common on these external lending markets).

### Recommendation
Before issuing the CPI, query the external protocol's remaining deposit capacity (Kamino reserve deposit cap / total available liquidity limit, or Drift spot market `max_token_deposits`) and clamp `amount` to the remaining capacity (or return early / cap similarly to how `get_remaining_deposit_capacity` is used in `lending_account_deposit`), so a near-cap deposit degrades gracefully instead of reverting the whole instruction.

### Proof of Concept
1. An external Kamino reserve (or Drift spot market) backing a marginfi Kamino/Drift bank is configured with (or organically reaches) a deposit cap close to its current total deposits.
2. A user calls `kamino_deposit`/`drift_deposit` with an `amount` that, combined with the reserve's/market's current deposits, exceeds that external cap.
3. `cpi_kamino_deposit`/`cpi_drift_deposit` reverts because the external program enforces its own limit, causing the entire marginfi instruction (including the token transfer bundled in the same transaction) to fail with no graceful partial-deposit fallback, unlike the native deposit path's `deposit_up_to_limit` behavior.

### Citations

**File:** programs/marginfi/src/instructions/kamino/deposit.rs (L72-77)
```rust
    if refresh_reserve {
        ctx.accounts.cpi_refresh_reserve()?;
    }

    ctx.accounts.cpi_transfer_user_to_obligation_owner(amount)?;
    ctx.accounts.cpi_kamino_deposit(amount, authority_bump)?;
```

**File:** programs/marginfi/src/instructions/kamino/deposit.rs (L299-337)
```rust
    pub fn cpi_kamino_deposit(&self, amount: u64, authority_bump: u8) -> MarginfiResult {
        let deposit_accounts = DepositReserveLiquidityAndObligationCollateral {
            owner: self.liquidity_vault_authority.to_account_info(),
            obligation: self.integration_acc_2.to_account_info(),
            lending_market: self.lending_market.to_account_info(),
            lending_market_authority: self.lending_market_authority.to_account_info(),
            reserve: self.integration_acc_1.to_account_info(),
            reserve_liquidity_mint: self.mint.to_account_info(),
            reserve_liquidity_supply: self.reserve_liquidity_supply.to_account_info(),
            reserve_collateral_mint: self.reserve_collateral_mint.to_account_info(),
            reserve_destination_deposit_collateral: self
                .reserve_destination_deposit_collateral
                .to_account_info(),
            user_source_liquidity: self.liquidity_vault.to_account_info(),
            placeholder_user_destination_collateral: None,
            collateral_token_program: self.collateral_token_program.to_account_info(),
            liquidity_token_program: self.liquidity_token_program.to_account_info(),
            instruction_sysvar_account: self.instruction_sysvar_account.to_account_info(),
        };

        // --- optional “farms_accounts” group ---
        let farms_accounts = DepositFarmsAccounts {
            obligation_farm_user_state: optional_account!(self.obligation_farm_user_state),
            reserve_farm_state: optional_account!(self.reserve_farm_state),
        };

        // --- wrap both groups in the outer struct ---
        let accounts = DepositReserveLiquidityAndObligationCollateralV2 {
            deposit_accounts,
            deposit_farms_accounts: farms_accounts,
            farms_program: self.farms_program.to_account_info(),
        };
        let program = self.kamino_program.to_account_info();
        let signer_seeds: &[&[&[u8]]] =
            bank_signer!(BankVaultType::Liquidity, self.bank.key(), authority_bump);
        let cpi_ctx = CpiContext::new_with_signer(program.key(), accounts, signer_seeds);
        deposit_reserve_liquidity_and_obligation_collateral_v2(cpi_ctx, amount)?;
        Ok(())
    }
```

**File:** programs/marginfi/src/instructions/drift/deposit.rs (L60-74)
```rust
    ctx.accounts.cpi_update_spot_market_cumulative_interest()?;
    let expected_scaled_balance_change = ctx
        .accounts
        .integration_acc_1
        .load()?
        .get_scaled_balance_increment(amount)?;

    let initial_scaled_balance = {
        let integration_acc_2 = ctx.accounts.integration_acc_2.load()?;
        integration_acc_2.get_scaled_balance(market_index)
    };

    ctx.accounts.cpi_transfer_user_to_liquidity_vault(amount)?;
    ctx.accounts
        .cpi_drift_deposit(market_index, amount, authority_bump)?;
```

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L76-80)
```rust
    let deposit_amount = if deposit_up_to_limit {
        amount.min(bank.get_remaining_deposit_capacity()?)
    } else {
        amount
    };
```
