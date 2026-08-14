### Title
First-depositor share-price inflation via permissionless `lending_pool_emissions_deposit` on a freshly-created bank - ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs])

### Summary
`lending_pool_emissions_deposit` is a permissionless instruction that lets *any* caller transfer real tokens straight into a bank's `liquidity_vault` and have that amount folded directly into `asset_share_value`, without minting new shares. [1](#0-0)  This is functionally the same "donate into the pool to inflate the exchange rate" primitive described in the external report (there, inflating `currentBalanceUSDT` via `openTrade`). Because the instruction only requires `total_asset_shares > 0` (i.e., at least one existing depositor) rather than requiring multiple/diversified depositors, a single attacker who is the sole depositor of a brand-new bank can inflate the share price against themselves-then-recoup it from a subsequent victim's deposit due to floor-rounding on share issuance.

### Finding Description
Share accounting in marginfi is driven by `bank.asset_share_value`, which is normally only updated through interest accrual (`calc_interest_rate_accrual_state_changes` in `interest_rate.rs`) — not by the raw token balance of the vault. This design protects against the classical "donate tokens directly to the vault" ERC4626 inflation attack, since `get_asset_amount`/`get_asset_shares` derive from the stored `asset_share_value`, not from `liquidity_vault`'s SPL balance. [2](#0-1) 

However, `lending_pool_emissions_deposit` reintroduces exactly this donation vector deliberately: it transfers `amount` tokens into `liquidity_vault` and then recomputes
`asset_share_value = (get_asset_amount(total_asset_shares) + amount) / total_asset_shares`
— i.e., it inflates the share value in direct proportion to a real token donation, with no requirement that more than one depositor exists (it only checks `total_asset_shares > 0`). [3](#0-2) 

Deposits mint shares via floor division (`bank.get_asset_shares`, called from `increase_balance_internal`), so a victim depositing into an already-inflated bank suffers rounding loss that is captured by the existing share holder(s): [4](#0-3) [5](#0-4) 

Attack sequence on a freshly initialized bank:
1. Attacker deposits the smallest possible amount (e.g. 1 native unit) as the *first and only* depositor, receiving `total_asset_shares = 1`, `asset_share_value = 1.0`.
2. Attacker calls `lending_pool_emissions_deposit` with a large amount of their own tokens, e.g. `N`. Since attacker owns 100% of `total_asset_shares`, this is a no-loss "loan to self" that simply resets `asset_share_value` to `(1 + N) / 1 = N + 1`, but the tokens are still fully attributable to the attacker's 1 share — no value has moved yet.
3. A victim, unaware of the manipulated exchange rate, deposits a real amount `V` (e.g. `2N`). Shares minted = `floor(V / asset_share_value) = floor(2N / (N+1))` ≈ `1`, i.e. the victim's actual value is rounded down hard, losing close to half their deposit's worth of shares.
4. Attacker withdraws their 1 share, now worth `(N + 1 + V) / (total_shares)` ≈ `1.5N`, extracting far more than they put in (`N+1`), with the surplus taken from the victim's deposit.

### Impact Explanation
This results in a genuine value transfer from victim depositors to the attacker — theft of deposited funds via rounding-loss extraction, matching the "unauthorized transfer of value" bar. It also constitutes a permanent economic loss for any victim who deposits shortly after a newly created bank is manipulated this way, since their share allocation is permanently under-valued relative to their contribution.

### Likelihood Explanation
The likelihood is constrained mainly by opportunity: the attacker needs to be the sole depositor of a bank at the moment of the emissions donation, and a victim must deposit into that same bank while the price is still inflated (before interest accrual or another neutral depositor dilutes the effect). This is realistic for a newly-listed or low-liquidity/permissionless bank (marginfi supports `lending_pool_add_bank_permissionless`), where an attacker can watch for bank creation and front-run the first real depositor. It is less practical against long-established, high-TVL banks with many existing depositors, since the emissions-deposit only "no-loss" resets the price when the attacker is a large fraction of `total_asset_shares`. The `EmissionsUpdateError` guard (`total_asset_shares > 0`) explicitly permits acting on a bank with just a single depositor, so nothing structurally prevents the scenario. However, I was not able to fully verify (in the available index) whether there is a minimum first-deposit-size guard or a require-non-zero-shares check on the mint side (`increase_balance_internal`) that might partially mitigate small-amount abuse; the search for `ZERO_AMOUNT_THRESHOLD`/minimum-deposit checks in `marginfi_account.rs` returned many matches, but I could not confirm their exact effect on the first-deposit share-minting path within the remaining exploration budget.

### Recommendation
- Require `lending_pool_emissions_deposit` to only operate when the bank has a minimum number of distinct depositors / a minimum `total_asset_shares` value (not just `> 0`), to prevent a sole depositor from manipulating `asset_share_value` before other users interact with the bank.
- Alternatively, mirror the Uniswap V2 / ERC4626 mitigation directly: on the very first deposit into a bank, mint a small amount of "dead" shares to a burn address (or otherwise seed `total_asset_shares` with protocol funds) so that `asset_share_value` cannot be manipulated to an attacker-favorable ratio by a single actor.
- Enforce `require!(asset_shares_increase != 0, MarginfiError::ZeroSharesMinted)` in `increase_balance_internal` so that any deposit landing in a rounding "dead zone" reverts instead of silently minting near-zero shares for a large deposit.
- Consider adding a maximum allowable single-transaction change (or minimum bank-age / minimum TVL threshold) to `lending_pool_emissions_deposit` amount relative to current `total_asset_shares`, or restrict it further so it cannot be used to jump the exchange rate by large multiples in one call.

### Proof of Concept
Conceptual sequence (not executed, derived from code paths cited above):
1. `bank_deposit(attacker, amount=1)` → `bank.total_asset_shares = 1`, `bank.asset_share_value = 1`.
2. `lending_pool_emissions_deposit(bank, amount=N)` funded by attacker → `bank.asset_share_value = (1 + N) / 1 = N + 1`. Uses [6](#0-5) .
3. `bank_deposit(victim, amount=2N)` → minted shares `= floor(2N / (N+1)) ≈ 1` via [5](#0-4) , versus the "fair" ~2 shares victim should get.
4. `bank_withdraw_all(attacker)` → attacker redeems 1 share at the post-victim-deposit share value, extracting more value than `N+1`, funded by the victim's under-credited deposit.

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

**File:** programs/marginfi/src/state/bank.rs (L237-256)
```rust
    fn get_asset_amount(&self, shares: I80F48) -> MarginfiResult<I80F48> {
        Ok(shares
            .checked_mul(self.asset_share_value.into())
            .ok_or_else(math_error!())?)
    }

    fn get_liability_shares(&self, value: I80F48) -> MarginfiResult<I80F48> {
        Ok(value
            .checked_div(self.liability_share_value.into())
            .ok_or_else(math_error!())?)
    }

    fn get_asset_shares(&self, value: I80F48) -> MarginfiResult<I80F48> {
        if self.asset_share_value == I80F48::ZERO.into() {
            return Ok(I80F48::ZERO);
        }
        Ok(value
            .checked_div(self.asset_share_value.into())
            .ok_or_else(math_error!())?)
    }
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1855-1860)
```rust
        let asset_shares_increase = bank.get_asset_shares(asset_amount_increase)?;
        balance.change_asset_shares(asset_shares_increase)?;
        bank.change_asset_shares(
            asset_shares_increase,
            matches!(operation_type, BalanceIncreaseType::BypassDepositLimit),
        )?;
```
