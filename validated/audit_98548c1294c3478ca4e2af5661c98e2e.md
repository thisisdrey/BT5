### Title
Reentrancy window in `juplend_deposit` due to state updates occurring after external CPI/transfer calls, with no reentrancy guard and no mutable borrow held during the external calls - (File: `programs/marginfi/src/instructions/juplend/deposit.rs`)

### Summary
`juplend_deposit` releases its mutable borrows on the `bank` and `marginfi_account` accounts before performing the underlying token transfer and the JupLend CPI call, and only re-acquires them afterward to credit shares and emit the deposit event. This mirrors the Sandclock `sponsor()`/`mint()` bug class described in the reference report: state-changing calls happen before the corresponding local ledger state is updated, and the update happens only after the external call returns, with no reentrancy guard covering the intervening window.

### Finding Description
In `juplend_deposit` [1](#0-0) , the function first loads `bank` and `marginfi_account` read-only in a scope that is dropped before any CPI is made. It then:
1. Calls `cpi_update_rate()`.
2. Computes `expected_shares` from a read-only load of the JupLend `lending` account (dropped immediately after).
3. Reads `pre_f_token_balance` and calls `cpi_transfer_user_to_liquidity_vault(amount)` followed by `cpi_juplend_deposit(amount, authority_bump)` [2](#0-1) .
4. Only after both CPIs return does it `load_mut()` the `bank` and `marginfi_account` to credit shares (`bank_account.deposit_no_repay`), record inflow, update the bank cache, and emit `LendingAccountDepositEvent` [3](#0-2) .

Critically, no mutable borrow of `bank` or `marginfi_account` is held while the `transfer_checked` (underlying transfer) and JupLend `deposit` CPI execute. This differs from the standard `lending_account_deposit` and `lending_account_withdraw` instructions, where `bank_loader.load_mut()` and `marginfi_account_loader.load_mut()` are acquired and held for the *entire* function body, including across the `deposit_spl_transfer`/`withdraw_spl_transfer` calls [4](#0-3) [5](#0-4) . In those two instructions, Anchor's `RefCell`-backed `AccountLoader` borrow acts as an incidental reentrancy guard: any CPI callback attempting to `load`/`load_mut` the same bank or account during the transfer would fail with an already-borrowed error. `juplend_deposit` lacks this protection because the borrows are dropped before the CPIs are issued.

Since `signer_token_account`/`liquidity_vault` are `InterfaceAccount<TokenAccount>` using `TokenInterface` [6](#0-5) , the underlying mint can be a Token-2022 mint. The codebase demonstrates first-class support for Token-2022 transfer-hook mints elsewhere (`test-utils/src/spl.rs`, `programs/test_transfer_hook/src/lib.rs`, `configure_bank.rs`), confirming transfer-hook-enabled mints are a supported/expected configuration in this program, not merely theoretical. If a JupLend bank's underlying mint carries a transfer hook, `cpi_transfer_user_to_liquidity_vault` will invoke the hook program during step 3, at a point where neither the `bank` nor the `marginfi_account` is locked, opening a window for a reentrant call into marginfi using stale on-chain state (the deposit's shares have not yet been credited, but tokens may already be in flight).

### Impact Explanation
This is a structural asymmetry versus the standard deposit/withdraw paths and matches the reported bug class (external call precedes local state update, no reentrancy guard). The direct impact is a break in the assumption that "no other marginfi instruction can execute against this bank/account mid-flight," which the standard SPL deposit/withdraw paths guarantee via Anchor's account borrow lock but the JupLend integration path does not. I could not fully confirm within the available index whether the pre/post fToken-balance equality check (`require_eq!(minted_shares, expected_shares, ...)`) at the end of the function [2](#0-1)  would deterministically catch and revert every possible reentrant manipulation, so I cannot claim a fully proven fund-drain path with certainty — this reduces confidence relative to a fully demonstrated PoC.

### Likelihood Explanation
Exploitability depends on whether a JupLend bank can be configured (or already is configured) with a Token-2022 mint that has an attacker-influenced transfer hook. This condition could not be independently confirmed against `configure_bank.rs`/bank-init constraints in the time available, which limits certainty of real-world reachability.

### Recommendation
Hold the mutable borrows on `bank` and `marginfi_account` for the entire duration of `juplend_deposit` (as is already done in `lending_account_deposit`/`lending_account_withdraw`), or add an explicit reentrancy flag (similar to `ACCOUNT_IN_FLASHLOAN`) that is set before the external transfer/CPI and cleared only after local state is finalized, rejecting any nested marginfi instruction while the flag is set.

### Proof of Concept
Not independently reproduced; the analysis is based on static comparison of borrow lifetimes between `juplend_deposit` [7](#0-6)  and `lending_account_deposit`/`lending_account_withdraw` [8](#0-7) [9](#0-8) , plus confirmation that Token-2022 transfer-hook mints are a supported feature in this codebase (`test-utils/src/spl.rs`, `programs/test_transfer_hook/src/lib.rs`). A concrete end-to-end exploit (malicious hook program reentering `juplend_deposit`/another instruction to produce an accounting mismatch that survives the `require_eq!` check) was not constructed or verified.

### Citations

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L41-129)
```rust
pub fn juplend_deposit(ctx: Context<JuplendDeposit>, amount: u64) -> MarginfiResult {
    let authority_bump: u8;
    {
        let marginfi_account = ctx.accounts.marginfi_account.load()?;
        let bank = ctx.accounts.bank.load()?;
        authority_bump = bank.liquidity_vault_authority_bump;

        validate_asset_tags(&bank, &marginfi_account)?;
        validate_bank_state(&bank, InstructionKind::FailsIfPausedOrReduceState)?;
    }

    // Refresh the exchange price (interest/rewards) for this slot.
    ctx.accounts.cpi_update_rate()?;

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

        bank.update_bank_cache(&group)?;

        marginfi_account.last_update = clock.unix_timestamp as u64;
        marginfi_account.lending_account.sort_balances();
        marginfi_account.sync_indexer_flags();

        emit!(LendingAccountDepositEvent {
            header: AccountEventHeader {
                signer: Some(ctx.accounts.authority.key()),
                marginfi_account: ctx.accounts.marginfi_account.key(),
                marginfi_account_authority: marginfi_account.authority,
                marginfi_group: marginfi_account.group,
            },
            bank: ctx.accounts.bank.key(),
            mint: bank.mint,
            amount,
            share_amount: share_amount.into(),
        });
    }

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L174-192)
```rust
    /// Owned by authority, the source account for the token deposit.
    #[account(mut)]
    pub signer_token_account: Box<InterfaceAccount<'info, TokenAccount>>,

    /// The bank's liquidity vault authority PDA (acts as signer for JupLend CPIs).
    /// NOTE: JupLend marks the signer as writable in their deposit instruction.
    #[account(
        mut,
        seeds = [
            LIQUIDITY_VAULT_AUTHORITY_SEED.as_bytes(),
            bank.key().as_ref(),
        ],
        bump = bank.load()?.liquidity_vault_authority_bump
    )]
    pub liquidity_vault_authority: SystemAccount<'info>,

    /// Bank liquidity vault (holds underlying mint and is used as depositor_token_account).
    #[account(mut)]
    pub liquidity_vault: Box<InterfaceAccount<'info, TokenAccount>>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L33-139)
```rust
pub fn lending_account_deposit<'info>(
    mut ctx: Context<'info, LendingAccountDeposit<'info>>,
    amount: u64,
    deposit_up_to_limit: Option<bool>,
) -> MarginfiResult {
    let LendingAccountDeposit {
        marginfi_account: marginfi_account_loader,
        authority: signer,
        signer_token_account,
        liquidity_vault: bank_liquidity_vault,
        token_program,
        bank: bank_loader,
        group: marginfi_group_loader,
        ..
    } = ctx.accounts;
    let clock = Clock::get()?;
    let maybe_bank_mint = utils::maybe_take_bank_mint(
        &mut ctx.remaining_accounts,
        &*bank_loader.load()?,
        token_program.key,
    )?;
    let deposit_up_to_limit = deposit_up_to_limit.unwrap_or(false);

    let mut bank = bank_loader.load_mut()?;
    let mut marginfi_account = marginfi_account_loader.load_mut()?;
    let group = marginfi_group_loader.load()?;
    validate_asset_tags(&bank, &marginfi_account)?;
    validate_bank_state(&bank, InstructionKind::FailsIfPausedOrReduceState)?;

    check!(
        !marginfi_account.get_flag(ACCOUNT_DISABLED)
            // Sanity check: liquidation doesn't allow the deposit ix, but just in case
            && !marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP),
        MarginfiError::AccountDisabled
    );

    bank.accrue_interest(
        clock.unix_timestamp,
        &group,
        #[cfg(not(feature = "client"))]
        bank_loader.key(),
    )?;

    let deposit_amount = if deposit_up_to_limit {
        amount.min(bank.get_remaining_deposit_capacity()?)
    } else {
        amount
    };

    if deposit_amount == 0 {
        return Ok(());
    }

    let mut bank_account = BankAccountWrapper::find_or_create(
        &bank_loader.key(),
        &mut bank,
        &mut marginfi_account.lending_account,
    )?;

    let share_amount = bank_account.deposit(I80F48::from_num(deposit_amount))?;
    marginfi_account.last_update = clock.unix_timestamp as u64;

    record_deposit_inflow(
        &mut bank,
        &group,
        marginfi_group_loader.key(),
        bank_loader.key(),
        marginfi_account.account_flags,
        deposit_amount,
        &clock,
    )?;
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

    bank.update_bank_cache(&group)?;
    emit!(LendingAccountDepositEvent {
        header: AccountEventHeader {
            signer: Some(signer.key()),
            marginfi_account: marginfi_account_loader.key(),
            marginfi_account_authority: marginfi_account.authority,
            marginfi_group: marginfi_account.group,
        },
        bank: bank_loader.key(),
        mint: bank.mint,
        amount: deposit_amount,
        share_amount: share_amount.into(),
    });

```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L45-247)
```rust
pub fn lending_account_withdraw<'info>(
    mut ctx: Context<'info, LendingAccountWithdraw<'info>>,
    amount: u64,
    withdraw_all: Option<bool>,
) -> MarginfiResult {
    let LendingAccountWithdraw {
        marginfi_account: marginfi_account_loader,
        destination_token_account,
        liquidity_vault: bank_liquidity_vault,
        token_program,
        bank_liquidity_vault_authority,
        bank: bank_loader,
        group: marginfi_group_loader,
        ..
    } = ctx.accounts;
    let clock = Clock::get()?;

    let withdraw_all = withdraw_all.unwrap_or(false);
    let mut marginfi_account = marginfi_account_loader.load_mut()?;
    let group = marginfi_group_loader.load()?;

    {
        let maybe_bank_mint = {
            let bank = bank_loader.load()?;
            utils::maybe_take_bank_mint(&mut ctx.remaining_accounts, &bank, token_program.key)?
        };

        let in_receivership_or_order_execution =
            marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP | ACCOUNT_IN_ORDER_EXECUTION);
        let mut bank = bank_loader.load_mut()?;
        validate_bank_state(&bank, InstructionKind::FailsInPausedState)?;

        // Fetch oracle price for rate limiting and deleverage tracking
        // When group rate limiter is enabled, oracle is required
        let group_rate_limit_enabled = group.rate_limiter.is_enabled();
        let price = if in_receivership_or_order_execution || group_rate_limit_enabled {
            let price = fetch_asset_price_for_bank_low_bias(
                &bank_loader.key(),
                &bank,
                &clock,
                ctx.remaining_accounts,
            )?;

            // Validate price is non-zero during liquidation/deleverage to prevent exploits
            if in_receivership_or_order_execution {
                check!(price > I80F48::ZERO, MarginfiError::ZeroAssetPrice);
            }

            price
        } else {
            I80F48::ZERO
        };

        bank.accrue_interest(
            clock.unix_timestamp,
            &group,
            #[cfg(not(feature = "client"))]
            bank_loader.key(),
        )?;

        let liquidity_vault_authority_bump = bank.liquidity_vault_authority_bump;

        let in_receivership = marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP);
        let lending_account = &mut marginfi_account.lending_account;
        let mut bank_account =
            BankAccountWrapper::find(&bank_loader.key(), &mut bank, lending_account)?;

        let (amount_pre_fee, share_amount) = if withdraw_all {
            // Note: In liquidation, we still want this passed on the books
            bank_account.withdraw_all(in_receivership)?
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
        bank.update_bank_cache(&group)?;

        emit!(LendingAccountWithdrawEvent {
            header: AccountEventHeader {
                signer: Some(ctx.accounts.authority.key()),
                marginfi_account: marginfi_account_loader.key(),
                marginfi_account_authority: marginfi_account.authority,
                marginfi_group: marginfi_account.group,
            },
            bank: bank_loader.key(),
            mint: bank.mint,
            amount: amount_pre_fee,
            share_amount: share_amount.into(),
            close_balance: withdraw_all,
        });
    }

    let mut health_cache = HealthCache::zeroed();
    health_cache.timestamp = clock.unix_timestamp;

    marginfi_account.lending_account.sort_balances();
    marginfi_account.sync_indexer_flags();

    // To update the bank's price cache
    let maybe_price: Option<OraclePriceWithMultiplier>;
    let bank_pk = bank_loader.key();

    // Note: during receivership and order execution, we skip all health checks until the end of the transaction.
    if !marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP | ACCOUNT_IN_ORDER_EXECUTION) {
        // Check account health, if below threshold fail transaction
        // Assuming `ctx.remaining_accounts` holds only oracle accounts
        // Uses heap-efficient health check to support accounts with up to 16 positions
        check_account_init_health(
            &marginfi_account,
            ctx.remaining_accounts,
            &mut Some(&mut health_cache),
        )?;
        health_cache.program_version = PROGRAM_VERSION;

        health_cache.set_engine_ok(true);
        marginfi_account.health_cache = health_cache;
    }

    // Fetch unbiased price for cache update
    // Note: during receivership, callers may omit oracle accounts; the cache simply won't update.
    {
        let bank = bank_loader.load()?;
        maybe_price =
            fetch_unbiased_price_for_bank_cache(&bank_pk, &bank, &clock, ctx.remaining_accounts)
                .ok();
    }

    bank_loader.load_mut()?.update_cache_price(maybe_price)?;

    Ok(())
}
```
