### Title
Front-runnable `lending_pool_emissions_deposit` allows flash-depositors to snipe same-bank emissions rewards meant for long-term depositors - ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs])

### Summary
The RocketPool report describes a reward model where a participant's share of a reward pool is computed from a **snapshot of stake held at the moment of `claim()`**, rather than from how long the stake was actually provided. This lets an attacker inflate their stake immediately before the reward is distributed, capture a disproportionate share, then immediately unwind the position. Marginfi's permissionless same-bank emissions mechanism, `lending_pool_emissions_deposit`, has the identical root cause: it instantly redistributes deposited "emissions" tokens to all depositors strictly in proportion to `total_asset_shares` held **at the instant the instruction executes**, with no time-weighting, vesting, or minimum holding period.

### Finding Description
`lending_pool_emissions_deposit` is a **permissionless** instruction. Anyone can call it to top up a bank's liquidity vault with the bank's own mint, and the code immediately raises `asset_share_value` for the whole pool based on the current `total_asset_shares`: [1](#0-0) 

Because share value is bumped for the entire pool based on a single instantaneous snapshot of `total_asset_shares`, a user who deposits a large amount of the bank's asset immediately before this call receives shares at the pre-reward price, and those same shares are revalued upward the instant the reward lands — exactly like RocketPool's node operator staking RPL right before `claim()`. There is no lockup imposed on regular `lending_account_deposit`/`lending_account_withdraw` (see the plain, un-throttled withdraw flow), so the attacker can withdraw immediately afterward, realizing the gain in one shot: [2](#0-1) 

Mathematically, if `V` is the existing pool value, `S` existing shares, and an attacker deposits `D` right before an emissions deposit of `E`:
- Attacker's shares: `D·S/V`
- New share price after emissions_deposit: `(V+D+E)/(S + D·S/V)`
- Attacker's realized value: `D·(V+D+E)/(V+D)`, i.e. profit `≈ D·E/(V+D)`

As `D → ∞` relative to `V`, the attacker's captured share of `E` approaches 100%, regardless of having provided zero liquidity/duration prior to the reward. This is a permissionless, unprivileged-user-reachable, core-accounting path (bank share pricing), directly matching the reported "reward captured by adding stake just before claim" bug class.

### Impact Explanation
Emissions/incentive campaigns (see `guides/USER/EMISSIONS.md`) are advertised as being earned "on a pro-rata basis in real time" by genuine depositors. The same-bank emissions instant-snapshot mechanic breaks this invariant: it lets an opportunistic flash-depositor divert most of a funded reward drop away from the depositors who actually supplied liquidity for the campaign duration. This is an unauthorized/unfair transfer of value intended for legitimate long-term depositors to a short-term depositor providing no real service to the pool — the same "no-service reward capture" impact called out in the source report. While it does not directly cause insolvency, it undermines the fairness/incentive-integrity of same-bank emissions campaigns and can be repeated every time an emissions top-up occurs.

### Likelihood Explanation
`lending_pool_emissions_deposit` is fully permissionless and requires no special privilege to call or to front-run — any depositor watching the mempool (or a party who is themselves periodically funding the campaign) can time a large deposit immediately before the call and withdraw immediately after. Deposit/withdraw actions have no cool-down, and the calculation is a single unweighted instant, so the exploit is mechanically simple to execute and repeatable each time a same-bank emissions deposit is made. The main constraint is having (or flash-borrowing) sufficient capital `D` relative to pool size `V` to dominate the share ratio, and deposit/borrow limits on the bank could reduce, but not eliminate, the attack's effectiveness.

### Recommendation
Do not let a single `lending_pool_emissions_deposit` instantly reprice all current shares based on a point-in-time snapshot. Instead:
- Stream/vest the emissions deposit over time (similar to the JupLend `rewards_rate_model` accrual pattern already used elsewhere in the codebase, where rewards accrue continuously and are only materialized on rate refresh) rather than applying the entire boost to `asset_share_value` in one atomic step.
- Alternatively, require a minimum holding/lockup period on deposits that are eligible for a given emissions deposit, or snapshot eligible balances prior to announcing/queuing the reward so freshly-added capital cannot capture it.
- Consider rate-limiting or restricting `lending_pool_emissions_deposit` to trusted/admin callers with an accrual schedule, rather than a fully permissionless, instantaneous distribution.

### Proof of Concept
1. Bank `B` has existing depositors with total value `V` and shares `S` (share price `v = V/S`).
2. Attacker observes (or is about to submit) a pending `lending_pool_emissions_deposit(E)` call for `B` (e.g., a scheduled reward top-up funded by the protocol/campaign sponsor).
3. Attacker calls `lending_account_deposit` with a large amount `D` (via own capital or a flashloan) just before that call lands, receiving `D/v` shares at the pre-reward price — see standard deposit path referenced by [3](#0-2)  (checks only that `total_asset_shares > 0`, no cooldown/time-weighting).
4. The `lending_pool_emissions_deposit(E)` call executes, and per lines 138-146 of `configure_bank.rs`, `asset_share_value` for the entire bank is bumped in one atomic step based on current `total_asset_shares`.
5. Attacker immediately calls `lending_account_withdraw` (no lockup enforced, as shown in `withdraw.rs`) to realize `≈ D·E/(V+D)` of the emissions reward — capturing most of `E` while having provided liquidity for a negligible duration, analogous to the RocketPool node operator staking RPL immediately before `claim()` and unwinding right after.

### Citations

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

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L45-103)
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
```
