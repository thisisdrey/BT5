### Title
Permissionless `lending_pool_emissions_deposit` Enables Sandwich Extraction of Yield via Instant `asset_share_value` Markup - (File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs)

### Summary
The External Report describes a sandwich attack in which an operator-posted, discrete yield/balance update (`onUnderlyingBalanceUpdate`) instantly and predictably raises a vault's share price, letting an attacker front-run the update with a large deposit and back-run it with a withdrawal to capture yield they never helped generate. Marginfi has a structurally analogous mechanism: `lending_pool_emissions_deposit`, which is **permissionless**, transfers tokens straight into the bank's `liquidity_vault`, and immediately recomputes `bank.asset_share_value` for *all* existing depositors in the same instruction [1](#0-0) .

### Finding Description
`lending_pool_emissions_deposit` lets any signer (`depositor`) push same-mint tokens into a bank's liquidity vault to reward depositors "through asset share value" [2](#0-1) . The handler:
1. Accrues interest first, then transfers `amount` into `liquidity_vault`.
2. Recomputes `asset_share_value` as `(total_assets + amount) / total_asset_shares` — an instantaneous, discontinuous jump applied to every existing share holder, exactly like `onUnderlyingBalanceUpdate`'s effect on `totalAssets()`/share price in the reported bug [3](#0-2) .

Because this instruction is a normal transaction (not an atomic multi-instruction CPI tied 1:1 to deposits), any observer can see a pending `lending_pool_emissions_deposit` (or trigger one themselves after observing available `emissions_funding_account` balance/incentive schedules) and:
- front-run it with `lending_account_deposit` into the same bank to acquire a large proportional share of `total_asset_shares` right before the markup,
- let the emissions-deposit transaction land, which raises `asset_share_value` for all current shareholders including the attacker's freshly minted shares,
- immediately back-run with `lending_account_withdraw` (optionally `withdraw_all`) to realize the gain.

This mirrors the root cause identified in the report: a permissionless, discrete "balance update" instruction that atomically and predictably repriced shares without any minimum holding period, vesting, or time-weighting, allowing an unprivileged actor to capture value contributed by others between two of their own transactions [4](#0-3) . Marginfi's regular `accrue_interest` path is not vulnerable to this class of attack because interest accrues continuously as a function of `time_delta` and utilization (share price cannot jump discontinuously without time passing) [5](#0-4) , and the external-integration price multipliers (Kamino/Drift/JupLend) are refreshed atomically inside the same deposit/withdraw instruction via CPI (`updateRate`), leaving no separate front-runnable transaction that reprices existing shares [6](#0-5) . `lending_pool_emissions_deposit`, by contrast, is exactly the kind of standalone, permissionless, share-price-jumping instruction the report warns about.

### Impact Explanation
An attacker who front-runs a `lending_pool_emissions_deposit` with a deposit and back-runs it with a withdrawal captures a disproportionate share of the emissions deposit relative to depositors who held their position throughout the accrual period — an unearned wealth transfer from long-term depositors/reward funders to the attacker, directly analogous to the "High" severity finding in the reference report (dilution of legitimate participants' yield). This is a real fund transfer (theft of yield) rather than a theoretical/no-impact issue.

### Likelihood Explanation
`lending_pool_emissions_deposit` is explicitly permissionless and callable by any `depositor` signer funding it from their own account [7](#0-6) , and deposit/withdraw are unprivileged user instructions with no cooldown or minimum holding period found in the codebase. The only friction is that the attacker must observe/predict the emissions-deposit transaction and possess enough capital to dominate `total_asset_shares` at that moment, and pay two transaction fees (deposit + withdraw) — standard MEV economics identical to the reference report's cost model. Likelihood is Medium: the attack is technically straightforward and reachable, but depends on emissions deposits being sizeable/regular enough and mempool/leader visibility on Solana (which differs from Ethereum but analogous same-slot ordering/bundling risk exists).

### Recommendation
- Require a minimum holding period (e.g., time-weighted or vesting-based) before newly deposited shares are eligible to receive `lending_pool_emissions_deposit` markups, or
- Distribute emissions deposits via a streaming/linear-vesting mechanism (similar to continuous interest accrual) instead of an instant lump-sum share-value markup, or
- Restrict `lending_pool_emissions_deposit` to a privileged/admin-only or rate-limited caller and require it to be bundled atomically with a snapshot of eligible depositors rather than a live `asset_share_value` recompute.

### Proof of Concept
1. Bank `B` has `total_asset_shares = S`, `asset_share_value = V`, so `total_assets = S*V`.
2. Attacker observes (or can trigger the conditions for) a pending `lending_pool_emissions_deposit(amount=D)` call on bank `B` [8](#0-7) .
3. Attacker submits `lending_account_deposit` for a large amount `X` just before it, minting `X/V` shares, making `total_asset_shares = S + X/V`.
4. `lending_pool_emissions_deposit(D)` executes: `asset_share_value` becomes `(S*V + X + D) / (S + X/V)` [3](#0-2) , instantly appreciating the attacker's newly-minted shares by a slice of `D` proportional to `X/(S*V+X)`.
5. Attacker immediately calls `lending_account_withdraw` (or `withdraw_all`) to redeem `X` plus their share of `D`, walking away with profit `≈ D * X/(S*V+X)` minus gas, without having contributed to the bank's utilization/interest generation that funded `D`.
6. Test scaffolding in the repo (`try_emissions_deposit`, `emissions_same_bank_deposit_updates_asset_share_value`) confirms the exact mechanics of the instant share-value markup exploited here [4](#0-3) .

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L84-156)
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

    bank.update_bank_cache(&group)?;

    msg!(
        "Deposited {} same-bank emissions into liquidity vault",
        amount
    );

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L158-192)
```rust
#[derive(Accounts)]
pub struct LendingPoolEmissionsDeposit<'info> {
    #[account(
        constraint = (
            !group.load()?.is_protocol_paused()
        ) @ MarginfiError::ProtocolPaused
    )]
    pub group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        mut,
        has_one = group @ MarginfiError::InvalidGroup,
        has_one = mint @ MarginfiError::InvalidEmissionsMint,
        has_one = liquidity_vault @ MarginfiError::InvalidLiquidityVault,
        constraint = is_marginfi_asset_tag(bank.load()?.config.asset_tag)
            @ MarginfiError::WrongAssetTagForStandardInstructions,
    )]
    pub bank: AccountLoader<'info, Bank>,

    pub mint: InterfaceAccount<'info, Mint>,

    /// NOTE: This is a TokenAccount, spl transfer will validate it.
    ///
    /// CHECK: Account provided only for funding rewards
    #[account(mut)]
    pub emissions_funding_account: UncheckedAccount<'info>,

    #[account(mut)]
    pub depositor: Signer<'info>,

    #[account(mut)]
    pub liquidity_vault: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Interface<'info, TokenInterface>,
}
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

**File:** programs/marginfi/src/state/bank.rs (L511-575)
```rust
    fn accrue_interest(
        &mut self,
        current_timestamp: i64,
        group: &MarginfiGroup,
        #[cfg(not(feature = "client"))] bank: Pubkey,
    ) -> MarginfiResult<()> {
        #[cfg(all(not(feature = "client"), feature = "debug"))]
        sol_log_compute_units();

        let time_delta: u64 = (current_timestamp - self.last_update).try_into().unwrap();
        if time_delta == 0 {
            return Ok(());
        }

        let total_assets = self.get_asset_amount(self.total_asset_shares.into())?;
        let total_liabilities = self.get_liability_amount(self.total_liability_shares.into())?;

        self.last_update = current_timestamp;

        if (total_assets == I80F48::ZERO) || (total_liabilities == I80F48::ZERO) {
            #[cfg(not(feature = "client"))]
            emit!(LendingPoolBankAccrueInterestEvent {
                header: GroupEventHeader {
                    marginfi_group: self.group,
                    signer: None
                },
                bank,
                mint: self.mint,
                delta: time_delta,
                fees_collected: 0.,
                insurance_collected: 0.,
            });

            return Ok(());
        }
        let ir_calc = self
            .config
            .interest_rate_config
            .create_interest_rate_calculator(group);

        let InterestRateStateChanges {
            new_asset_share_value: asset_share_value,
            new_liability_share_value: liability_share_value,
            insurance_fees_collected,
            group_fees_collected,
            protocol_fees_collected,
        } = calc_interest_rate_accrual_state_changes(
            time_delta,
            total_assets,
            total_liabilities,
            &ir_calc,
            self.asset_share_value.into(),
            self.liability_share_value.into(),
        )?;

        debug!("deposit share value: {}\nliability share value: {}\nfees collected: {}\ninsurance collected: {}",
            asset_share_value, liability_share_value, group_fees_collected, insurance_fees_collected);

        self.cache.accumulated_since_last_update = asset_share_value
            .checked_sub(I80F48::from(self.asset_share_value))
            .and_then(|v| v.checked_mul(I80F48::from(self.total_asset_shares)))
            .ok_or_else(math_error!())?
            .into();
        self.cache.interest_accumulated_for = time_delta.min(u32::MAX as u64) as u32;
        self.asset_share_value = asset_share_value.into();
```

**File:** guides/DEVELOPERS_INTEGRATORS/JUPLEND_INTEGRATION.md (L25-28)
```markdown
- Refreshing Lending - The "Lending" account stores information about the exchange rate of
  fTokens/underlying. The refresh instruction is called `updateRate`. Our `juplend_deposit` and
  `juplend_withdraw` handlers call this internally. For liquidation and other risk-sensitive flows,
  include `updateRate` for all involved Juplend banks in the same tx before health checks that read JupLend state.
```
