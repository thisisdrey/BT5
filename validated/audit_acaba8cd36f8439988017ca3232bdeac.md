### Title
JIT deposit sniping of same-mint emissions rewards via permissionless `lending_pool_emissions_deposit` - ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs])

### Summary
`lending_pool_emissions_deposit` instantly and atomically bumps `bank.asset_share_value` for the *entire* pool of current asset-shareholders, pro-rata to shares held at the exact moment the instruction executes, with no minimum holding period or timestamp check. This mirrors the `LimitOrderHook` bug class: value that accrued/was funded for existing long-term liquidity providers can be intercepted by an attacker who deposits large capital immediately before the reward-funding transaction and withdraws immediately after, capturing a disproportionate share of rewards for effectively zero time-at-risk.

### Finding Description
`lending_pool_emissions_deposit` is explicitly documented and implemented as permissionless: "Permissionlessly deposit same-mint emissions directly into the bank liquidity vault, increasing depositor value through asset share value" [1](#0-0) .

The instruction reads the current `total_asset_shares`, transfers `amount` of the underlying mint into the liquidity vault, and immediately recomputes `bank.asset_share_value` as `(total_assets + amount) / total_asset_shares` [2](#0-1) . Every asset-share holder at that instant benefits proportionally — there is no snapshot of "who held shares before the deposit was queued/announced" and no lockup or timestamp check tying eligibility to a longer holding period.

Anchor's `Balance` struct does track `last_update` for a position, but this is used only for a *separate*, off-chain-computed incentive-token emissions system (documented in `guides/USER/EMISSIONS.md`) — it does not gate the on-chain, real-time `asset_share_value` bump performed by `lending_pool_emissions_deposit` [3](#0-2) .

Existing tests confirm this real-time, pro-rata mechanic precisely: two depositors (40/60 split) each immediately receive their proportional share of a $50 emissions deposit the instant it lands, with no vesting or delay [4](#0-3) .

This is the direct analog of the Uniswap `LimitOrderHook` bug: fees/value that accrue for the benefit of the *existing* liquidity providers can be diverted to a party who becomes a liquidity provider only for the instant the value-increasing event occurs, then exits. Concretely:

1. Attacker monitors the mempool/observes a scheduled or foreseeable `lending_pool_emissions_deposit` call (e.g., a foundation/partner topping up rewards for a bank).
2. Immediately before that transaction lands (or in the same transaction if composable), attacker deposits a very large amount into the same bank via the ordinary `lending_pool_deposit` path, capturing a majority of `total_asset_shares`.
3. The emissions deposit executes, raising `asset_share_value` for all current shareholders — the attacker now owns the majority of `total_asset_shares` and thus captures the majority of the reward.
4. Attacker immediately withdraws, realizing the reward gain net of trivial fees, having held the position for effectively zero economic time and having contributed nothing to the value being distributed.

### Impact Explanation
Genuine long-term depositors have their expected pro-rata share of same-mint emissions/reward funding diluted and effectively stolen by a JIT depositor who never bore any real risk or time exposure in the pool. This is a concrete unauthorized transfer of value (theft) from legitimate depositors to the attacker, funded by whoever supplies the emissions deposit (protocol, partner, or foundation), analogous in mechanism and impact to the reported Uniswap fee-theft bug.

### Likelihood Explanation
The instruction is fully permissionless and has no anti-sniping controls (no time-weighted average shares, no minimum holding period, no deposit caps tied to reward events, no slot/timestamp restriction). Any user who can observe or predict an emissions-funding transaction (which is by design periodic/expected, per `guides/USER/EMISSIONS.md`) can execute this with an ordinary deposit followed by a withdrawal, requiring no special privileges — just capital and timing (e.g., via mempool observation or predictable funding schedules), making this readily exploitable by any unprivileged user with sufficient capital and MEV/front-running capability.

### Recommendation
Do not distribute same-mint emissions purely pro-rata to instantaneous share balance. Instead:
- Track a time-weighted average balance (or a minimum eligible holding period, e.g., shares held since before the reward-funding block) for eligibility in `lending_pool_emissions_deposit` distributions, or
- Snapshot `total_asset_shares` and per-user shares prior to accepting the emissions deposit and require deposits made after the snapshot to only receive future proportional value, or
- Introduce a deposit fee/cooldown for freshly deposited shares to reduce the profitability of JIT deposits into emissions-eligible pools.

### Proof of Concept
1. Attacker watches for an upcoming `lending_pool_emissions_deposit` call on a bank with modest `total_asset_shares` (e.g., as in the `emissions_same_bank_deposit_updates_asset_share_value` test setup) [5](#0-4) .
2. Just before that transaction, attacker deposits a large sum via `lending_pool_deposit`, becoming the majority `total_asset_shares` holder.
3. The emissions deposit executes: `bank.asset_share_value` increases uniformly for all shareholders based on `updated_total_assets / total_asset_shares` [6](#0-5) , so the attacker — now holding the majority of shares — receives the majority of the reward.
4. Attacker immediately calls `lending_pool_withdraw` to redeem shares at the new, inflated `asset_share_value`, realizing the reward gain instantly, while original depositors who provided liquidity for the entire accrual period are left with a diluted share of the reward.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L84-89)
```rust
/// Permissionlessly deposit same-mint emissions directly into the bank liquidity vault,
/// increasing depositor value through asset share value.
pub fn lending_pool_emissions_deposit(
    ctx: Context<LendingPoolEmissionsDeposit>,
    amount: u64,
) -> MarginfiResult {
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L111-146)
```rust
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

**File:** type-crate/src/types/user_account.rs (L294-303)
```rust
    /// The user's asset (deposit) shares in the bank. Multiply by `bank.asset_share_value` for
    /// the token amount.
    pub asset_shares: WrappedI80F48,
    /// The user's liability (borrow) shares in the bank. Multiply by `bank.liability_share_value`
    /// for the token amount.
    pub liability_shares: WrappedI80F48,
    /// Unclaimed emissions rewards for this position
    pub emissions_outstanding: WrappedI80F48,
    /// Unix timestamp (u64) of the last emissions calculation for this position
    pub last_update: u64,
```

**File:** programs/marginfi/tests/misc/emissions_deposit.rs (L211-243)
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
```

**File:** programs/marginfi/tests/misc/emissions_deposit.rs (L270-286)
```rust
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
