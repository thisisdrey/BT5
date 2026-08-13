## Valid Analog Found

### Title
Yield/Reward Stealing via Front-Run/Back-Run Sandwich of `lending_pool_emissions_deposit` - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
The marginfi program exposes a permissionless instruction, `lending_pool_emissions_deposit`, that injects "reward" tokens directly into a bank's liquidity vault and instantly re-prices `asset_share_value` for all existing depositors of that bank, in the exact same pattern described in the external StUSR report (a permissionless reward-distribution call that proportionally re-values existing shares). Because Solana transactions are visible in the public mempool before landing, an attacker can front-run this call with a large deposit and back-run it with a withdrawal to capture a disproportionate share of the injected value, diluting/harming honest existing depositors, exactly like sandwiching `RewardDistributor.distribute()` in the StUSR report.

### Finding Description
`lending_pool_emissions_deposit` transfers `amount` tokens into the bank's `liquidity_vault` and then recomputes `bank.asset_share_value` as `(total_assets + amount) / total_asset_shares`: [1](#0-0) 

This is functionally identical to StUSR's `RewardDistributor.distribute()`: a permissionless call that raises the value-per-share for whoever is currently holding shares in the bank at call time, with no snapshotting, no cooldown, and no protection against last-block deposits.

The instruction has:
- No signer/authority restriction beyond `depositor: Signer<'info>` (any wallet can call it, and any wallet can fund it) — it is explicitly documented as "permissionless": [2](#0-1) 
- No minimum holding period / deposit lock before a user's shares can benefit from an emissions deposit and then be withdrawn.
- No restriction preventing a deposit and a subsequent emissions-deposit-triggered value bump and withdrawal from occurring within the same or adjacent transactions/slots.

Standard `lending_account_deposit` / `lending_account_withdraw` flows accrue interest first but otherwise allow deposit and withdrawal at will: [3](#0-2) [4](#0-3) 

This mirrors precisely the "become the primary stakeholder right before rewards land, withdraw right after" attack described in the StUSR report.

### Impact Explanation
An attacker monitoring the mempool for a pending `lending_pool_emissions_deposit` transaction (e.g., a group admin or reward program funding emissions/incentives into a bank) can:
1. Front-run with a large `lending_account_deposit` into the target bank, becoming the majority (or sole) shareholder just before the emissions deposit lands.
2. Let the emissions deposit execute, instantly raising `asset_share_value` proportionally to their now-dominant share.
3. Back-run with `lending_account_withdraw` (or `withdraw_all`) to realize the gain immediately.

This captures value that should have accrued to long-term depositors, at their expense — a direct value-transfer/theft from honest depositors to the attacker, and it disincentivizes genuine depositors from remaining staked, exactly the harm called out in the reference report.

### Likelihood Explanation
The instruction is permissionless and unauthenticated with respect to *who* triggers it (any funder can call `lending_pool_emissions_deposit` for a bank once emissions funding exists), and deposits/withdrawals into a bank are otherwise unrestricted and instantaneous. Any observer of the public mempool (or anyone aware that an emissions/incentive campaign deposit is about to occur, e.g., per a public schedule or an admin's known cadence) can execute the sandwich with ordinary deposit/withdraw instructions plus normal Solana transaction ordering/priority-fee competition. No private-mempool or anti-sandwich protection is implemented for this flow.

### Recommendation
- Require that `lending_pool_emissions_deposit` (and any similar reward-injection instruction) be executed via a mechanism resistant to sandwiching, such as a time-weighted/streaming reward accrual (rather than an instantaneous share-value bump) or a minimum holding-period/lock-up for shares to qualify for a given reward drop.
- Consider snapshotting share balances prior to the emissions deposit (e.g., based on a `last_update` cutoff) so that same-block/adjacent deposits cannot benefit from the injected value.
- Alternatively, restrict who may call `lending_pool_emissions_deposit`/require it be bundled atomically with fixed distribution logic that cannot be front-run (e.g., pro-rata calculated over a longer accrual window, similar to how `accrue_interest` streams value over `time_delta` rather than in a single lump-sum re-pricing).

### Proof of Concept
1. Bank B has depositor D0 holding all `total_asset_shares` with `asset_share_value = V0`.
2. Attacker observes a pending `lending_pool_emissions_deposit(amount=X)` transaction for Bank B in the mempool.
3. Attacker front-runs with `lending_account_deposit` depositing amount `Y >> D0's deposit`, becoming the majority shareholder of Bank B.
4. The emissions deposit executes: `bank.asset_share_value = (total_assets + X) / total_asset_shares`, per [5](#0-4) , raising value-per-share for all current holders, disproportionately benefiting the attacker's large stake.
5. Attacker back-runs with `lending_account_withdraw(withdraw_all=true)`, realizing the reward-driven gain instantly while original depositor D0 receives a diluted share of the same reward pool relative to their original stake duration. [6](#0-5)  confirms this exact share-value re-pricing mechanic and its proportional distribution to whoever holds shares at call time.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L84-146)
```rust
/// Permissionlessly deposit same-mint emissions directly into the bank liquidity vault,
/// increasing depositor value through asset share value.
pub fn lending_pool_emissions_deposit(
    ctx: Context<LendingPoolEmissionsDeposit>,
    amount: u64,
) -> MarginfiResult {
    if amount == 0 {
        return Ok(());
    }

    let clock = Clock::get()?;
    let mut bank = ctx.accounts.bank.load_mut()?;
    let group = ctx.accounts.group.load()?;

    utils::validate_bank_state(&bank, utils::InstructionKind::FailsIfPausedOrReduceState)?;

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

    let total_asset_shares = I80F48::from(bank.total_asset_shares);
    check!(
        total_asset_shares > I80F48::ZERO,
        MarginfiError::EmissionsUpdateError
    );

    bank.accrue_interest(
        clock.unix_timestamp,
        &group,
        #[cfg(not(feature = "client"))]
        ctx.accounts.bank.key(),
    )?;

    transfer_checked(
        CpiContext::new(
            ctx.accounts.token_program.key(),
            TransferChecked {
                from: ctx.accounts.emissions_funding_account.to_account_info(),
                to: ctx.accounts.liquidity_vault.to_account_info(),
                authority: ctx.accounts.depositor.to_account_info(),
                mint: ctx.accounts.mint.to_account_info(),
            },
        ),
        amount,
        ctx.accounts.mint.decimals,
    )?;

    let total_assets = bank.get_asset_amount(total_asset_shares)?;
    let updated_total_assets = total_assets
        .checked_add(I80F48::from_num(amount))
        .ok_or_else(math_error!())?;

    bank.asset_share_value = updated_total_assets
        .checked_div(total_asset_shares)
        .ok_or_else(math_error!())?
        .into();
```

**File:** programs/marginfi/src/lib.rs (L209-216)
```rust
    /// (permissionless) Deposit same-bank emissions directly into liquidity vault and increase
    /// depositors' value via `asset_share_value`.
    pub fn lending_pool_emissions_deposit(
        ctx: Context<LendingPoolEmissionsDeposit>,
        amount: u64,
    ) -> MarginfiResult {
        marginfi_group::lending_pool_emissions_deposit(ctx, amount)
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L69-93)
```rust
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
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L98-130)
```rust
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
```

**File:** programs/marginfi/tests/misc/emissions_deposit.rs (L211-286)
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

    let bank_after = usdc_bank.load().await;
    let shares_after = I80F48::from(bank_after.total_asset_shares);
    let share_value_after = I80F48::from(bank_after.asset_share_value);

    let liquidity_vault_after =
        TokenAccountFixture::fetch(test_f.context.clone(), bank_after.liquidity_vault)
            .await
            .balance()
            .await;

    let asset_shares_value_multiplier =
        1.0 + emissions_deposit as f64 / (depositor_a_amount + depositor_b_amount) as f64;

    assert_eq!(shares_after, shares_before);

    // Should be equal, zero liabilities are present
    assert_eq!(
        share_value_before
            .checked_mul(I80F48::from_num(asset_shares_value_multiplier))
            .unwrap(),
        share_value_after
    );
    assert_eq!(
        liquidity_vault_after - liquidity_vault_before,
        native!(emissions_deposit, "USDC")
    );
    assert_eq!(I80F48::from(bank_after.emissions_remaining), I80F48::ZERO);
```
