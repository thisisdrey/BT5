## Analysis Result [1](#0-0) 

### Title
Unfair emissions distribution allows front-running deposits to steal pro-rata rewards from long-term depositors - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
`lending_pool_emissions_deposit` instantly boosts a bank's `asset_share_value` pro-rata to whatever `total_asset_shares` exist at the moment the instruction executes, with no consideration for how long depositors have held their shares. Any unprivileged actor can deposit a large amount into the bank immediately before this permissionless emissions-injection transaction lands, capture a disproportionate share of the newly injected value, then withdraw immediately afterward — diluting the rewards that should accrue to depositors who supplied liquidity over time.

### Finding Description
`lending_pool_emissions_deposit` is a fully permissionless instruction (any signer can call it, funding it from their own token account) that deposits same-mint "emissions" directly into a bank's liquidity vault and recalculates `asset_share_value` as: [2](#0-1) 

```
total_asset_shares = bank.total_asset_shares  (current, instantaneous)
total_assets = get_asset_amount(total_asset_shares)
updated_total_assets = total_assets + amount
bank.asset_share_value = updated_total_assets / total_asset_shares
```

This is confirmed by the test suite, which shows the multiplier applied to `asset_share_value` is computed purely from the ratio of the emissions amount to whatever total was deposited at call time — with no time-weighting: [3](#0-2) 

Since `bank_account.deposit()` and `bank_account.withdraw()` in `lending_account_deposit`/`lending_account_withdraw` immediately mint/burn shares at the current `asset_share_value` with no lock-up, cooldown, or minimum holding period, a user can:
1. Deposit a large amount into the bank right before (or same-block as) a `lending_pool_emissions_deposit` transaction lands, becoming a large fraction of `total_asset_shares` at that instant.
2. Let the emissions deposit land, instantly boosting `asset_share_value` pro-rata to *current* shareholding, not to time held.
3. Immediately withdraw via `lending_account_withdraw` with `withdraw_all=true`, realizing an outsized share of the newly injected emissions value relative to depositors who held their position throughout the accrual period.

This exactly mirrors the reported bug class: rewards/emissions are distributed based on instantaneous share balance rather than duration/precedence of staking, letting a large short-term depositor "steal" rewards from long-term depositors. It also stands in contrast to marginfi's own time-weighted emissions design documented for the older "Campaign" emissions model, where rewards accrue continuously pro-rata over time and explicitly account for when a depositor joined: [4](#0-3) . `lending_pool_emissions_deposit` bypasses that fairness property entirely by applying a lump-sum, instant, non-time-weighted value bump.

No mitigating cooldown, minimum staking duration, or same-block/flashloan-style restriction exists on deposit/withdraw around this instruction; the only relevant flags gate other unrelated states (flashloan, receivership, frozen, disabled) via `check_account_init_health`/`account_flags`, none of which block this sequence for a normal signer.

### Impact Explanation
An unprivileged actor can extract value that is economically meant for existing long-term depositors, effectively diluting/stealing part of the yield legitimately earned by depositors who held their position during the accrual period. This is a concrete unauthorized transfer of value between depositors (not merely theoretical), reachable purely through standard user-facing instructions (`lending_account_deposit`, `lending_pool_emissions_deposit`, `lending_account_withdraw`), with no admin privilege required by the attacker.

### Likelihood Explanation
Likelihood depends on `lending_pool_emissions_deposit` calls being predictable/visible ahead of confirmation (e.g., via mempool/public scheduling of incentive campaigns) and the attacker having enough capital to become a large fraction of `total_asset_shares` at that moment. Given Solana's public transaction visibility and the permissionless, un-gated nature of this instruction, this is practically executable whenever an emissions-funding party (protocol, partner, or the group) deposits a lump sum of incentives into an active bank.

### Recommendation
Time-weight emissions distribution (e.g., stream emissions and update `asset_share_value` gradually per the existing continuous-accrual "Campaign" emissions model already used elsewhere) rather than applying a single instantaneous pro-rata boost to whatever shares exist at call time. Alternatively, gate deposits shortly before/after an emissions deposit (e.g., require a minimum holding period before a deposit counts toward capturing that emissions distribution) or restrict `lending_pool_emissions_deposit` to a time-locked/streamed release similar to Sushiswap MasterChef/Synthetix-style accounting.

### Proof of Concept
1. Bank `B` has `total_asset_shares = S0` held by long-term depositor `D` (deposited `A0` long ago).
2. Attacker `X` calls `lending_account_deposit` with a large `amount` right before a known/anticipated `lending_pool_emissions_deposit(amount_emissions)` call, becoming a large fraction of `total_asset_shares` (now `S0 + Sx`).
3. `lending_pool_emissions_deposit` executes: `asset_share_value` increases by factor `(total_assets + amount_emissions) / total_assets`, applied uniformly to all shares including `X`'s freshly-minted ones. [5](#0-4) 
4. `X` immediately calls `lending_account_withdraw` with `withdraw_all=true`, realizing `Sx * new_asset_share_value` tokens — capturing a share of `amount_emissions` proportional to `Sx/(S0+Sx)` despite having contributed capital for effectively zero time, at the expense of `D`'s expected share of the emissions.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L84-86)
```rust
/// Permissionlessly deposit same-mint emissions directly into the bank liquidity vault,
/// increasing depositor value through asset share value.
pub fn lending_pool_emissions_deposit(
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

**File:** programs/marginfi/tests/misc/emissions_deposit.rs (L260-286)
```rust
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

**File:** guides/USER/EMISSIONS.md (L10-21)
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
