## Title
Front-running `lending_pool_emissions_deposit` with a large deposit dilutes and steals rewards from existing depositors - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
`lending_pool_emissions_deposit` is a fully **permissionless** instruction that pushes same-mint token rewards directly into a bank's liquidity vault and raises `asset_share_value` for every existing depositor of that bank proportionally to their share of `total_asset_shares` at the moment the instruction executes. [1](#0-0)  Because the transfer amount and destination bank are visible in the mempool before confirmation (exactly the "off-chain event becomes visible on-chain before settlement" pattern described in the Pod report), an attacker can front-run the emissions deposit with a large `lending_account_deposit` into the same bank, capture a disproportionate share of the newly injected rewards, and then withdraw immediately afterward — diluting and effectively stealing value from the depositors who actually funded the bank during the period the rewards accrued for.

### Finding Description
`lending_pool_emissions_deposit` computes the new share value as:

```
new_share_value = (total_assets_before + emissions_amount) / total_asset_shares_before
```

using `total_asset_shares` and `total_assets` read at execution time. [2](#0-1)  The instruction has no restriction on who may call it (documented explicitly as "permissionless" both in code comments and patch notes) and carries no check that depositors have been present for any minimum duration. [3](#0-2) 

`lending_account_deposit` (the standard user deposit path) is likewise unprivileged and unrestricted by any pause/cooldown related to pending emissions — it only checks bank pause/reduce-only state and account flags. [4](#0-3)  There is no `pauseDepositsDuringAwarding`-style guard analogous to the one recommended in the external Pod report to block deposits while a value-injecting operation is pending.

An attacker who observes an unconfirmed `lending_pool_emissions_deposit` transaction in the mempool can:
1. Submit `lending_account_deposit` with a large amount into the same bank, landing before the emissions transaction (or bundled ahead of it).
2. Let the emissions deposit execute, which recomputes `asset_share_value` using the now-inflated `total_asset_shares` that includes the attacker's freshly minted shares.
3. Immediately withdraw via `lending_account_withdraw`, which has no cooldown or lockup on the standard bank path — deposit-then-withdraw within the same or immediately following transaction is explicitly exercised and accepted elsewhere in the test suite. [5](#0-4) 

This nets the attacker a slice of the reward proportional to their now-inflated share count, while permanently diluting the reward-per-share that legitimate, pre-existing depositors receive — the same dilution/theft mechanic as the Pod winner-frontrunning bug, just applied to marginfi's emissions-injection mechanism instead of a lottery payout.

### Impact Explanation
This results in **unauthorized transfer of value** from legitimate long-duration depositors to an opportunistic front-runner: the attacker earns yield they did not economically deserve (no capital at risk over time), and honest depositors receive a diluted `asset_share_value` increase. This satisfies the "concrete theft / unauthorized transfer" bar for unprivileged-user paths in `lending_account_deposit`, `lending_account_withdraw`, and `lending_pool_emissions_deposit` — all of which are reachable without any privileged role.

### Likelihood Explanation
Likelihood is moderate: it requires (a) knowledge that an emissions deposit transaction is pending in the mempool or a scheduled/predictable emissions cadence, and (b) enough capital to dominate `total_asset_shares` at the moment of execution. Both conditions are realistic for a well-capitalized MEV searcher monitoring the (public) marginfi program for `lending_pool_emissions_deposit` calls, especially for smaller/thinner banks where a modest deposit can capture a large proportion of shares.

### Recommendation
Introduce a mechanism to prevent large, transient deposits from capturing freshly-injected emissions, mirroring the `pauseDepositsDuringAwarding`-style fix recommended in the external report. Concrete options:
- Require a minimum holding duration (time-weighted shares) before a deposit is eligible to receive a proportional share of an emissions deposit, or
- Snapshot eligible shares/holders prior to executing `lending_pool_emissions_deposit` (e.g., only shares that existed as of the last accrual or N slots prior), or
- Add a short deposit-cooldown / anti-sandwich check specifically gating deposits into a bank that has an emissions deposit in flight in the same block/slot.

### Proof of Concept
1. Bank `B` has depositors A and C holding `total_asset_shares = S` worth `total_assets = V` (`asset_share_value = V/S`).
2. Observe an unconfirmed `lending_pool_emissions_deposit(amount = E)` transaction targeting bank `B` in the mempool.
3. Front-run with `lending_account_deposit(amount = X)` from attacker wallet, minting `X / (V/S)` new shares before the emissions transaction lands, so `total_asset_shares' = S + X/(V/S)`.
4. Emissions transaction executes: `new_share_value = (V + X + E) / (S + X/(V/S))`, per `lending_pool_emissions_deposit`'s share-value formula. [6](#0-5) 
5. Attacker immediately calls `lending_account_withdraw` to redeem `X` shares at `new_share_value`, netting a portion of `E` proportional to `X/(S + X/(V/S))` despite having deposited only moments earlier — value that would otherwise have accrued entirely to A and C.

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

**File:** patch-note-drafts/patch-notes-0.1.9.md (L151-155)
```markdown
### Emissions

- `lending_pool_emissions_deposit(amount)` (permissionless) — deposit same-bank emissions directly
  into the liquidity vault, raising `asset_share_value`.

```

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L56-92)
```rust
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
```

**File:** tests/specs/basic/17_rateLimiter.spec.ts (L677-710)
```typescript
  it("(user 2) deposit offsets withdraw outflow", async () => {
    await setRateLimits({
      bankHourly: usdcNative(1),
      bankDaily: new BN(0),
      groupHourly: new BN(0),
      groupDaily: new BN(0),
    });

    // Deposit 1 USDC (inflow)
    await userProgram().provider.sendAndConfirm(
      new Transaction().add(
        await depositIx(userProgram(), {
          marginfiAccount: requireWithdrawAccount(),
          bank: bankKeypairUsdc.publicKey,
          tokenAccount: rateLimitUser.usdcAccount,
          amount: usdcNative(1),
          depositUpToLimit: false,
        }),
      ),
    );

    // Withdraw 2 USDC (outflow)
    await userProgram().provider.sendAndConfirm(
      new Transaction().add(
        await withdrawIx(userProgram(), {
          marginfiAccount: requireWithdrawAccount(),
          bank: bankKeypairUsdc.publicKey,
          tokenAccount: rateLimitUser.usdcAccount,
          remaining: usdcOnlyRemainingAccounts(),
          amount: usdcNative(2),
        }),
      ),
    );

```
