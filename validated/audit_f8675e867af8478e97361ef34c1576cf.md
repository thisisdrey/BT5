### Title
Reward sandwiching in `lending_pool_emissions_deposit` lets a flash-depositor steal a share of same-bank emissions from real depositors — ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs])

### Summary
`lending_pool_emissions_deposit` boosts `bank.asset_share_value` for every share-holder based purely on the instantaneous `total_asset_shares` at the moment the emissions transfer lands, with no minimum holding period or time-weighting. This is the same root-cause pattern as the reported Trove `dailyMintReward` bug: rewards are attributed by a point-in-time balance snapshot instead of a time-weighted position, so anyone who deposits immediately before the reward-crediting instruction and withdraws immediately after captures a disproportionate share of the reward at the expense of depositors who actually held funds in the bank over time.

### Finding Description
`lending_pool_emissions_deposit` is a **permissionless** instruction that transfers same-mint tokens into the bank's liquidity vault and immediately re-prices `asset_share_value` for the entire pool of asset shares that exist *right now*: [1](#0-0) 

Specifically:
```
let total_asset_shares = I80F48::from(bank.total_asset_shares);
...
let total_assets = bank.get_asset_amount(total_asset_shares)?;
let updated_total_assets = total_assets.checked_add(I80F48::from_num(amount))...;
bank.asset_share_value = updated_total_assets.checked_div(total_asset_shares)...;
``` [2](#0-1) 

Every existing holder of `total_asset_shares` at that exact instant is credited pro-rata with the entire `amount` deposited. There is no requirement that a depositor have held their shares for any minimum duration, and no vesting/streaming of the reward over time — exactly the flaw identified in the external report (`_updateIntegralForAccount` crediting a point-in-time `balance` rather than a time-weighted position).

Because ordinary `lending_account_deposit` / `lending_account_withdraw` on marginfi banks carry no entry/exit fee (fees only exist on borrow/origination, not on deposit/withdraw) and there is no cooldown between depositing and withdrawing, an attacker can:
1. Monitor (or predict, e.g. scheduled/cron-style "Campaign" emissions funding as described in the incentives model) an upcoming `lending_pool_emissions_deposit` call for a target bank.
2. Deposit a large amount `X` into that bank right before the emissions-deposit call lands, minting `X` in new asset shares against pre-existing pool total `T`.
3. Let the emissions deposit of `E` land, which raises `asset_share_value` proportionally for **all** current shares, giving the attacker `X/(T+X) * E` of the reward that otherwise would have gone entirely to the pre-existing depositors (`T/(T) * E` if the attacker hadn't intervened).
4. Immediately withdraw the full deposit plus the captured reward share.

This is a direct value transfer from the bank's genuine, time-invested lenders to the transient flash-depositor, with no cost to the attacker beyond gas/priority fees. `SECURITY.md` explicitly states flash-loan/oracle-manipulation-style attacks are **not** excluded from scope (`Note: This does not exclude oracle manipulation/flash-loan attacks.`), so this attacker profile is in-bounds.

Note: the separate weekly incentive/airdrop system described in `guides/USER/EMISSIONS.md` mitigates this class of issue by computing rewards pro-rata over real time (time-weighted), which is precisely the fix the external report recommends. However `lending_pool_emissions_deposit`'s "same-bank emissions" path bypasses that protection entirely and uses the vulnerable point-in-time model.

### Impact Explanation
Genuine long-term depositors in a bank running "same-bank emissions" campaigns lose a portion of rewards that should accrue to them, transferred instead to an opportunistic depositor who contributes zero economic time/risk. This is a concrete, repeatable value-theft primitive that scales with the size of each emissions deposit and the capital the attacker can marshal (which can itself be a flash-loaned amount from an external DEX/lending protocol, since marginfi deposit/withdraw carries no fee to disincentivize this).

### Likelihood Explanation
Moderate-to-high. The instruction is permissionless and callable by anyone, meaning campaign operators/admins doing periodic top-ups are a predictable, recurring target. An attacker does not need any privileged role — only capital (which can be borrowed) and the ability to time a deposit/withdraw around the emissions-deposit transaction. The larger the emissions deposit relative to existing TVL, the more profitable the sandwich.

### Recommendation
- Do not apply the emissions boost instantaneously and uniformly to all current share holders. Instead, stream/vest the emissions deposit over time (e.g., raise `asset_share_value` gradually via the existing interest-accrual mechanism) so that only time-weighted holders benefit.
- Alternatively, snapshot `total_asset_shares` from some point prior to the emissions deposit (e.g., minimum holding period, or use a TWAP of shares) rather than the shares outstanding at the exact instant of the call.
- Consider adding a minimum holding-period requirement, or a deposit/withdraw fee on emissions-bearing banks, to remove the profitability of atomic sandwich deposits.

### Proof of Concept
1. Bank `B` has `T` total asset shares deposited by legitimate lenders, `asset_share_value = V`.
2. Admin/campaign schedules `lending_pool_emissions_deposit(amount = E)` for bank `B` (predictable, since it's a recurring campaign, or simply observed as a pending transaction).
3. Attacker calls `lending_account_deposit` on bank `B` for amount `X` right before the emissions call executes, minting `X` new asset shares (total now `T + X`).
4. `lending_pool_emissions_deposit(E)` executes: `bank.asset_share_value` increases proportionally over `total_asset_shares = T + X`, so the attacker's `X` shares are credited `X/(T+X) * E` of the emission, which would otherwise have gone to the pre-existing `T` shares.
5. Attacker immediately calls `lending_account_withdraw` (withdraw_all) to redeem `X + X/(T+X)*E`, realizing `X/(T+X)*E` in profit with no holding period and (absent a deposit/withdraw fee) no cost besides gas.
6. Repeating this around every scheduled/observed emissions deposit systematically siphons reward value away from genuine long-term depositors in bank `B`.

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
