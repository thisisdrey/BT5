## Title
Yield/emissions theft via deposit-before-`lending_pool_emissions_deposit` sandwich, with no withdrawal timelock to prevent instant redemption - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
`lending_pool_emissions_deposit` is a permissionless instruction that increases a bank's `asset_share_value` pro-rata for *all current* depositors the instant it executes, funded by any signer. Since marginfi has no deposit lockup or withdrawal timelock, an attacker can deposit a large amount into the bank immediately before this (predictable, recurring) reward-funding call lands, and withdraw immediately after, capturing a slice of the freshly-added yield that should have accrued only to depositors who bore risk/duration in the pool — the same "mint-before-rebase, redeem-after" bug class described in the LRTOracle report.

### Finding Description
`lending_pool_emissions_deposit` transfers `amount` of the emissions mint from any `depositor: Signer<'info>` into the bank's `liquidity_vault`, then immediately recomputes `bank.asset_share_value` using the *current* `total_asset_shares`: [1](#0-0) 

The accounts struct shows this call is fully permissionless — any signer can be the funder, and no relationship to the bank admin, group admin, or existing depositors is required: [2](#0-1) 

Because `total_asset_shares` at the moment of the call determines how the newly-added reward is distributed (`bank.asset_share_value = updated_total_assets / total_asset_shares`), whoever holds shares *at that exact instant* — regardless of how long they've held them — receives a proportional cut of the reward. There is no vesting, cooldown, or minimum holding period tied to shares; deposits and withdrawals both take effect atomically and instantly via `lending_account_deposit`/`lending_account_withdraw`, with interest/emissions accrual (`bank.accrue_interest`) always running first in the same transaction: [3](#0-2) 

This mirrors the LRTOracle report's root cause: a value-increasing event (reward/rebase) is realized in a single, observable/predictable transaction, and there is no timelock preventing an attacker from instantaneously entering right before and exiting right after that transaction, letting them scoop rewards meant for genuine long-term depositors.

The same mechanical pattern (accrue-then-realize with no delay) also applies to ordinary interest accrual on banks that have been dormant for a long time: `bank.accrue_interest` computes and applies all elapsed interest in one shot the next time *any* user transacts with the bank, so a large "flash" deposit placed immediately before that triggering transaction, followed by an immediate withdrawal, captures a disproportionate share of the backlog interest that accrued while the attacker had no capital at risk.

### Impact Explanation
An attacker can extract value that rightfully belongs to depositors who provided liquidity/duration risk, diluting their yield. This is a direct, unauthorized transfer of economic value (yield stealing) from legitimate depositors to the attacker, achieved purely through transaction-timing manipulation rather than by contributing corresponding risk or duration — impact class matches "concrete theft" per the validation criteria. The magnitude scales with the size of the emissions/interest event and the size of the flash deposit the attacker can muster (up to the bank's deposit limit).

### Likelihood Explanation
`lending_pool_emissions_deposit` is explicitly documented as a recurring, permissionless reward-funding mechanism (see `guides/ADMIN/PERMISSIONS_AND_ROLES.md` referencing same-mint emissions and `guides/ADMIN/COLLECTING_FEES.md`), so its cadence is knowable/observable by anyone watching on-chain activity or admin tooling schedules. Because Solana deposit/withdraw/emissions-deposit are all single-instruction, same-block operations, an attacker can trivially compose deposit → (wait for or bundle with) emissions_deposit → withdraw within one or a few transactions, with no timelock blocking the exit. This makes the attack economically practical, especially for well-funded emissions campaigns.

### Recommendation
Introduce a minimum holding period (timelock) between deposit and withdrawal — or a decaying/vesting schedule for freshly deposited shares' eligibility to reward-bearing events like `lending_pool_emissions_deposit` — so that a deposit cannot both receive a reward distribution and be withdrawn immediately afterward. Alternatively, restrict `lending_pool_emissions_deposit`'s `depositor` to a trusted/admin-configured emissions authority and require rewards to stream in gradually (e.g., over a vesting window) rather than being applied to `asset_share_value` in a single atomic jump, removing the exploitable step-function event.

### Proof of Concept
1. Bank `B` has `total_asset_shares = 1000` (1000 tokens deposited by long-term depositors), `asset_share_value = 1`.
2. Attacker observes (or bundles) that `lending_pool_emissions_deposit(amount = 10)` is about to be called (per `configure_bank.rs`, callable by anyone, funding is same-mint rewards).
3. Attacker calls `lending_account_deposit` with `1000` tokens immediately before, doubling `total_asset_shares` to `2000` — no fee/timelock blocks this per `deposit.rs`.
4. `lending_pool_emissions_deposit(10)` executes: `assets_after = 2010`, `asset_share_value = 2010/2000 = 1.005`, applied uniformly to all 2000 shares including the attacker's freshly minted 1000.
5. Attacker immediately calls `lending_account_withdraw` for their full position (`1000 * 1.005 = 1005` tokens), netting `+5` tokens of profit that should have gone entirely to the original 1000-share depositors, who now only see `1000 * 1.005 - 1000 = 5` split among themselves instead of the full `10`.
6. No lockup, vesting, or cooldown exists in `lending_account_withdraw` to prevent step 5 from occurring in the same or next transaction as steps 3–4.

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
