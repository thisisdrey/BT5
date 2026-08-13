### Title
Missing Token-2022 transfer-fee adjustment in JupLend deposit path causes vault under-funding and share/asset mismatch - ([File: programs/marginfi/src/instructions/juplend/deposit.rs])

### Summary
The core marginfi deposit path (`lending_account_deposit`) explicitly compensates for Token-2022 transfer-fee tokens by computing a pre-fee transfer amount so the liquidity vault actually receives the exact `deposit_amount` that is credited as shares. The JupLend integration's deposit path does not perform this compensation: it transfers and CPIs with the raw `amount` parameter, assuming the vault receives exactly `amount`, which is false for a Token-2022 mint with an active `TransferFeeConfig`.

### Finding Description
In the core deposit instruction, marginfi computes `amount_pre_fee` from the mint's `TransferFeeConfig` before invoking the transfer, so that the vault ends up holding exactly `deposit_amount` (the value used to mint shares): [1](#0-0) 

This fee-aware logic lives in `calculate_pre_fee_spl_deposit_amount` / `calculate_post_fee_spl_deposit_amount`, which explicitly branch on `TransferFeeConfig` extension data: [2](#0-1) 

The JupLend deposit instruction, however, transfers the raw `amount` with a plain `transfer_checked` call (no fee compensation) and then immediately CPIs into JupLend's `deposit` using that same raw `amount`: [3](#0-2) [4](#0-3) 

If the bank's underlying mint is Token-2022 with a nonzero transfer fee, the actual amount the `liquidity_vault` receives from the user is `amount - fee`, strictly less than `amount`. The subsequent `cpi_juplend_deposit(amount, ...)` moves `amount` out of the same vault into the fToken vault — an amount the vault never fully received from this transaction. `record_deposit_inflow` and `expected_shares_for_deposit_from_rates` are also computed against the nominal `amount`, not the post-fee amount actually received: [5](#0-4) 

Depending on whether the vault happens to hold surplus balance from other flows, this either:
- reverts the whole transaction (insufficient vault balance for the JupLend CPI), causing depositors to be unable to use JupLend banks backed by fee-bearing T22 mints, or
- silently succeeds by drawing on the vault's other balance (e.g., residual dust or another depositor's funds), causing `bank.total_asset_shares` / vault accounting to diverge from the real token balance actually contributed by this depositor — i.e., the bank credits shares as if `amount` was deposited when only `amount - fee` actually arrived, understating what backs other depositors' claims.

This exactly matches the reported bug class ("protocol assumes a full nominal amount was transferred, but fee-on-transfer tokens deliver less") that the marginfi core path was hardened against, but that hardening was not carried over to the JupLend integration path. Kamino/Drift `add_pool.rs` instructions likewise flag `IS_T22` on the bank but there is no equivalent fee-aware compensation visible in the reviewed JupLend deposit code.

### Impact Explanation
If a JupLend bank is backed by a Token-2022 mint that has (or later has enabled) a nonzero transfer fee, unprivileged depositors using `juplend_deposit`/`juplend_init_position` either cannot deposit at all (DoS) or, in the worse case where the vault carries surplus, the bank state accrues asset shares that are not fully backed by real vault tokens — an accounting mismatch that can compound to insolvency for the bank's other depositors upon withdrawal. This differs from the original report's "permanently locked user funds" framing but shares the same root cause: assuming `amount` transferred equals `amount` received.

### Likelihood Explanation
This requires an admin to configure a JupLend bank on a Token-2022 mint that has (or is later configured with) a nonzero `TransferFeeConfig`. JupLend banks are documented as "first-party pools owned by mrgn," so mint selection is an admin/deployment decision rather than something a depositor controls; however, once such a bank exists, every unprivileged depositor's `juplend_deposit` call is affected. The likelihood is Medium-Low: it depends on an admin choosing (deliberately or by mistake) a fee-bearing T22 mint for a JupLend-tagged bank; the codebase's own tests (`tests/specs/juplend/jlr09_withT22.spec.ts`) exercise a T22 mint for JupLend but do not appear to configure a nonzero transfer fee, suggesting this specific combination is untested.

### Recommendation
Mirror the core deposit path's fee handling in `programs/marginfi/src/instructions/juplend/deposit.rs` (and `init_position.rs`): before transferring, compute the pre-fee amount via `calculate_pre_fee_spl_deposit_amount` (or measure the vault's balance before/after the transfer) so that `expected_shares`, `record_deposit_inflow`, and the downstream `cpi_juplend_deposit` all use the amount actually received by the vault, not the nominal `amount` requested. Alternatively, explicitly reject bank creation/deposit for JupLend (and other integration) banks when the underlying Token-2022 mint has a nonzero transfer fee, consistent with how `lending_pool_emissions_deposit` already rejects nonzero-fee mints with `InvalidTransfer`.

### Proof of Concept
Not independently executable from the indexed code alone; the exploit path is inferable from code inspection:
1. Admin creates a JupLend bank (`lending_pool_add_bank_juplend`) on a Token-2022 mint that has (or later gets configured with) a nonzero `TransferFeeConfig`.
2. A user calls `juplend_deposit(amount)`. `cpi_transfer_user_to_liquidity_vault(amount)` moves `amount` from the user, but the vault only receives `amount - fee` due to the T22 transfer fee.
3. `cpi_juplend_deposit(amount, ...)` attempts to move the full nominal `amount` out of the vault into the JupLend fToken vault.
4. If the vault has no surplus, this step fails with an SPL insufficient-funds error, reverting the deposit (DoS). If the vault has surplus token balance from other sources, the CPI succeeds using that surplus, while `expected_shares`/`record_deposit_inflow` still credit shares based on the full nominal `amount`, understating the actual net contribution and mismatching bank vault accounting versus recorded shares. Confirming the exact on-chain consequence (revert vs. silent shortfall) would require running the JupLend deposit flow against a T22 mint with `transferFeeBasisPoints > 0`, which is not present in the currently indexed test suite for JupLend.

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

**File:** programs/marginfi/src/utils/general.rs (L65-112)
```rust
pub fn calculate_pre_fee_spl_deposit_amount(
    mint_ai: AccountInfo,
    post_fee_amount: u64,
    epoch: u64,
) -> MarginfiResult<u64> {
    if mint_ai.owner.eq(&Token::id()) {
        return Ok(post_fee_amount);
    }

    let mint_data = mint_ai.try_borrow_data()?;
    let mint = StateWithExtensions::<spl_token_2022::state::Mint>::unpack(&mint_data)?;

    match mint.get_extension::<TransferFeeConfig>() {
        Ok(transfer_fee_config) => {
            let epoch_fee = transfer_fee_config.get_epoch_fee(epoch);
            let pre_fee_amount = calculate_pre_fee_amount(epoch_fee, post_fee_amount).unwrap();
            Ok(pre_fee_amount)
        }
        Err(_) => Ok(post_fee_amount),
    }
}

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

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L55-106)
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

    let post_f_token_balance = accessor::amount(&ctx.accounts.integration_acc_2.to_account_info())?;
    let minted_shares = post_f_token_balance
        .checked_sub(pre_f_token_balance)
        .ok_or_else(|| error!(MarginfiError::MathError))?;

    // Exact match required.
    require_eq!(
        minted_shares,
        expected_shares,
        MarginfiError::JuplendDepositFailed
    );

    {
        let mut bank = ctx.accounts.bank.load_mut()?;
        let mut marginfi_account = ctx.accounts.marginfi_account.load_mut()?;
        let group = ctx.accounts.group.load()?;
        let clock = Clock::get()?;

        let mut bank_account = BankAccountWrapper::find_or_create(
            &ctx.accounts.bank.key(),
            &mut bank,
            &mut marginfi_account.lending_account,
        )?;

        let share_amount = bank_account.deposit_no_repay(I80F48::from_num(minted_shares))?;

        record_deposit_inflow(
            &mut bank,
            &group,
            ctx.accounts.group.key(),
            ctx.accounts.bank.key(),
            marginfi_account.account_flags,
            amount,
            &clock,
        )?;
```

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L253-265)
```rust
impl<'info> JuplendDeposit<'info> {
    pub fn cpi_transfer_user_to_liquidity_vault(&self, amount: u64) -> MarginfiResult {
        let program = self.token_program.to_account_info();
        let accounts = TransferChecked {
            from: self.signer_token_account.to_account_info(),
            to: self.liquidity_vault.to_account_info(),
            authority: self.authority.to_account_info(),
            mint: self.mint.to_account_info(),
        };
        let cpi_ctx = CpiContext::new(program.key(), accounts);
        transfer_checked(cpi_ctx, amount, self.mint.decimals)?;
        Ok(())
    }
```
