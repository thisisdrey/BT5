### Title
Emissions deposits into a bank can be sandwiched with flash deposits to dilute rewards for long-term depositors - (File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs)

### Summary
`lending_pool_emissions_deposit` is a permissionless instruction that injects same-mint emissions directly into a bank's liquidity vault and immediately re-prices `asset_share_value` for every current depositor, pro-rata to `total_asset_shares` at the instant the call executes. There is no mechanism (unbonding period, snapshot-of-depositors-at-intent-time, or time-weighting) that limits this pro-rata split to depositors who actually held their position for the accrual/incentive period. This mirrors the Acala incentives design flaw: a fixed reward amount is divided among whatever shares happen to be present at the moment of accumulation, so an unprivileged actor can deposit immediately before the reward injection and withdraw immediately after to capture an unfair share of the reward, diluting genuine long-term depositors.

### Finding Description
`lending_pool_emissions_deposit` transfers `amount` of the bank's mint into the `liquidity_vault` and recomputes `bank.asset_share_value` as `(total_assets + amount) / total_asset_shares`, using whatever `total_asset_shares` exists at call time: [1](#0-0) 

This is functionally identical to the Acala pattern flagged in the report: a fixed reward pool (the `amount` deposited) is split among "all deposited shares" at the moment of accumulation, with no requirement that shares have been held for any minimum duration. Ordinary bank deposit/withdraw actions used to acquire/release `total_asset_shares` are unprivileged and have no cooldown or lockup enforced anywhere in this flow, so an actor can:
1. Front-run a pending `lending_pool_emissions_deposit` transaction with a large `lending_account_deposit` into the same bank.
2. Let the emissions deposit execute, which raises `asset_share_value` proportionally across the now-inflated `total_asset_shares`.
3. Immediately withdraw, realizing a share of the emissions reward proportional to their transient stake rather than any genuine long-term deposit.

The `Balance.emissions_outstanding` / `last_update` mechanism used for the separate, rate-based "Campaign" emissions system is time-weighted per the design described in the emissions guide, and is not the vector at issue here: [2](#0-1) [3](#0-2) 

However, the "same-bank emissions deposit" flow, tested explicitly in `emissions_same_bank_deposit_updates_asset_share_value`, confirms the vulnerable pattern: the reward is split strictly by shares held at the moment of the deposit call, with no duration weighting: [4](#0-3) 

### Impact Explanation
Any unprivileged actor who can front-run/back-run a `lending_pool_emissions_deposit` call (which is itself a fully permissionless, unauthenticated instruction) can capture a disproportionate fraction of the injected reward relative to depositors who actually held their position throughout the relevant period. This directly dilutes the rewards intended for genuine long-term depositors, resulting in an unauthorized/unfair value transfer away from honest users toward transient MEV-style depositors — an economic loss to the pool's legitimate depositors that scales with the size of the emissions deposit and the attacker's ability to acquire large transient share of `total_asset_shares`.

### Likelihood Explanation
This requires the standard MEV assumptions noted in the original Acala judgment: visibility of the pending `lending_pool_emissions_deposit` transaction (mempool/validator visibility or predictable timing), and availability of capital (or a flash loan of the underlying mint) to acquire a large transient deposit position risk-free within one or two transactions/blocks, since ordinary deposit/withdraw here have no cooldown. These are the same non-trivial but not unreasonable assumptions that the original Code4rena judge accepted as sufficient for a valid Medium.

### Recommendation
- Snapshot `total_asset_shares` (or require pool-eligible depositors to have held shares for a minimum duration) at the time emissions deposits are "announced" rather than at execution time, or
- Time-weight `asset_share_value` increases based on how long each depositor has held their position since the last emissions deposit, analogous to the time-weighted approach already used for the rate-based emissions Campaign system, or
- Add a minimum holding period / cooldown for deposits before they are eligible to participate in a same-bank emissions deposit's pro-rata split.

### Proof of Concept
1. Attacker monitors the mempool/validator for a pending `lending_pool_emissions_deposit(bank, amount)` transaction.
2. Attacker submits `lending_account_deposit` with a large amount into the same bank just prior, inflating `total_asset_shares`.
3. `lending_pool_emissions_deposit` executes: `bank.asset_share_value = (total_assets + amount) / total_asset_shares`, as shown in `lending_pool_emissions_deposit`. [5](#0-4) 
4. Attacker immediately calls `lending_account_withdraw`, realizing the appreciated `asset_share_value` gained from the emissions deposit despite holding the position only across two transactions — exactly the scenario the existing test `emissions_same_bank_deposit_updates_asset_share_value` demonstrates share-value uplift being split strictly by current-shares-at-call-time. [6](#0-5)

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

**File:** type-crate/src/types/user_account.rs (L294-305)
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
    /// Reserved for future use
    pub _padding: [u64; 1],
```

**File:** guides/USER/EMISSIONS.md (L9-21)
```markdown

For example, a Campaign might distribute 7 tokens of A to lenders per week (one per day). Each
lender's share is determined on a pro-rata basis in real time. If there are two lenders, each
depositing the same amount, then each will be 3.5 tokens per week.

Now let's say there are two users, the first one has \$1 in deposits. User 2 deposits \$1 on
Thursday, and \$5 more on Saturday. This means User 1 and 2 both get 0.5 tokens/day on Thursday and
Friday. On Saturday and beyond, User 1 gets $1/(1+6)= 0.143$ tokens, and User 2 gets $6/(1+6)=0.857$
tokens/day.

Emissions/incentives are delivered by airdrop to the Account's authority, typically on Wednesday, in
no particular order. In the above example, User 1 would get $0.5 + 0.5 * 0.143 * 5 = 1.715$ tokens
and User 2 would get $0.5 + 0.5 + 0.857 * 5 = 5.285$ tokens
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
