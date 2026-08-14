Confirmed: `LendingPoolEmissionsDeposit` has no restriction on who `depositor` is (any signer), no lockup/cooldown, and no deposit/withdraw fee mechanism exists in the codebase to penalize immediate withdrawal. This confirms the JIT MEV analog is reachable and unmitigated.

### Title
Permissionless `lending_pool_emissions_deposit` enables MEV/JIT sandwich attack to steal pro-rata share of emissions boost - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
The permissionless instruction `lending_pool_emissions_deposit` allows anyone to fund a bank's `liquidity_vault` with same-mint tokens, which immediately and atomically increases `bank.asset_share_value` in proportion to `total_asset_shares` at that instant [1](#0-0) . Because the value boost is distributed pro-rata over whoever holds `total_asset_shares` the moment the transaction lands, an attacker can front-run any pending `lending_pool_emissions_deposit` transaction with a large `lending_account_deposit`, capture a large fraction of the emissions-driven share-value increase, then immediately withdraw — exactly the JIT/MEV yield-skimming pattern described in the Sturdy report's `distributeYield` finding.

### Finding Description
`lending_pool_emissions_deposit` recomputes `asset_share_value` as `(total_assets + amount) / total_asset_shares` [2](#0-1) , i.e., the value bump is split among all currently-held asset shares of the bank, regardless of how recently those shares were minted. The instruction:
- Is permissionless — `depositor` is just a `Signer`, with no allow-list, minimum holding period, or snapshot of "shares held since last distribution" [3](#0-2) .
- Only requires `total_asset_shares > 0` [4](#0-3) , so a single JIT depositor minting shares right before this call is enough to qualify for a large chunk of the payout.
- There is no deposit/withdraw fee, cooldown, or minimum holding duration anywhere in the deposit/withdraw paths (`lending_account_deposit`, `lending_account_withdraw`) that would penalize instantly withdrawing right after the emissions bump lands.

This is a direct on-chain analog to Sturdy's `distributeYield`: an admin, bot, or any third party broadcasting a `lending_pool_emissions_deposit` transaction (visible in the mempool before landing) can be sandwiched by an attacker who (1) deposits a large amount into the target bank just before, (2) lets the emissions deposit boost `asset_share_value`, then (3) withdraws immediately, capturing a share of the airdropped emissions disproportionate to the time/value they actually provided to the pool, diluting the payout that should have gone to genuine long-term depositors.

### Impact Explanation
This causes a leak of value from legitimate long-term depositors to a JIT attacker: the emissions/incentive tokens (real economic value funded by the emissions campaign) are diverted pro-rata to capital that was never at risk and contributed nothing to the bank's utilization or liquidity depth. This matches the "value leaked from protocol/users via a hypothetical attack path with external requirements" class of Medium severity in the original finding, since it requires knowledge of a pending emissions-deposit tx and the ability to front-run/sandwich it (e.g., via Jito bundles on Solana).

### Likelihood Explanation
Likelihood is moderate: it requires (a) visibility into a pending `lending_pool_emissions_deposit` transaction and (b) the ability to land a deposit immediately before and a withdraw immediately after in the same or adjacent slot, which is achievable via Solana's Jito bundle infrastructure. Given `lending_pool_emissions_deposit` is intentionally permissionless (anyone can fund emissions, including third parties/partners), the number of potential trigger transactions and their visibility is higher than an admin-only scheduled call, increasing attack surface.

### Recommendation
Consider one or both of:
1. Restrict `lending_pool_emissions_deposit` to landing via non-mempool-visible paths (e.g., admin/keeper submits through private relays), though this only partially mitigates since any caller is currently permitted.
2. Introduce a snapshot/minimum-holding-period mechanism so that emissions/value boosts are apportioned based on share balances held since the last distribution/deposit event, rather than the balance at the exact moment funds land, preventing JIT depositors from capturing a disproportionate share.
3. Alternatively, add a deposit lockup or time-weighted share accounting specifically for emissions eligibility.

### Proof of Concept
1. Attacker monitors the mempool/Jito auction for a pending `lending_pool_emissions_deposit(amount)` transaction targeting bank `B` with `total_asset_shares = S` and `total_assets = A`.
2. Attacker submits a bundle:
   a. `lending_account_deposit` of a large amount `D` into bank `B`, minting `D` in new asset shares (bringing `total_asset_shares` to `S + D_shares`).
   b. The pending `lending_pool_emissions_deposit(amount)` executes, computing `new_share_value = (A + D + amount) / (S + D_shares)` [2](#0-1) , raising every depositor's share value including the attacker's freshly minted shares.
   c. Attacker immediately calls `lending_account_withdraw` to redeem `D_shares` at the new, boosted `asset_share_value`, extracting a portion of `amount` proportional to `D_shares / (S + D_shares)` despite having held the position for a single slot.
3. The attacker profits `amount * D_shares / (S + D_shares)` minus negligible gas/tip costs, while legitimate long-term depositors receive a correspondingly diluted share of the same emissions deposit.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L84-98)
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
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L111-115)
```rust
    let total_asset_shares = I80F48::from(bank.total_asset_shares);
    check!(
        total_asset_shares > I80F48::ZERO,
        MarginfiError::EmissionsUpdateError
    );
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L138-146)
```rust
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
