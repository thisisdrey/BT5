### Title
Permissionless `lending_pool_emissions_deposit` enables share-price inflation causing zero-share minting for depositors - ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs])

### Summary
The reported bug class (integer-division share minting that rounds to zero after an attacker inflates the pool's implicit exchange rate with a donation) maps to marginfi's `lending_pool_emissions_deposit` instruction. This instruction is explicitly documented as **permissionless** and directly raises `bank.asset_share_value` in proportion to an arbitrary attacker-supplied token amount, without requiring the caller to hold any privileged role. Because all subsequent deposits compute minted shares as `amount / asset_share_value` (an integer/fixed-point division), an attacker who is the sole (or dominant) shareholder of a low-liquidity bank can inflate `asset_share_value` so far that a legitimate depositor's subsequent deposit rounds down to (near) zero shares while their principal is absorbed into the vault, and its value accrues to existing share holders (the attacker).

### Finding Description
`lending_pool_emissions_deposit` is documented as: "Permissionlessly deposit same-mint emissions directly into the bank liquidity vault, increasing depositor value through asset share value." [1](#0-0) 

It requires only that `total_asset_shares > 0` (i.e., there is at least one existing shareholder) and then transfers an arbitrary `amount` from the caller into the bank's liquidity vault: [2](#0-1) 

It then directly recomputes `asset_share_value` as `(total_assets + amount) / total_asset_shares`: [3](#0-2) 

This is functionally the same primitive as the report's `totalBalance / currentShares` inflation vector: the exchange rate used to price all future deposits is derived from a value an unprivileged caller can unilaterally and disproportionately inflate relative to `total_asset_shares`.

All standard bank deposits compute minted shares from this same `asset_share_value` via `get_asset_shares`: `value / asset_share_value` [4](#0-3) , and this is exactly the code path exercised on every deposit through `increase_balance_internal`: [5](#0-4) 

**Attack sequence (analogous to the report):**
1. Attacker back-runs `add_bank`/bank listing (or targets an existing low-liquidity bank) and is the first/dominant depositor, obtaining `total_asset_shares` at a low, controlled value (initial mint is 1:1 against `asset_share_value` starting near 1, per `Bank::zeroed()` defaults used in tests) [6](#0-5) .
2. Attacker calls `lending_pool_emissions_deposit` with a very large `amount` relative to their own tiny share count, sharply inflating `asset_share_value`.
3. A legitimate user (Alice) then deposits `N` tokens. Her minted shares are computed as `N / asset_share_value`, which can round down toward zero for any `asset_share_value` large enough relative to `N`.
4. Alice's principal enters the vault (`total_assets` increases) but she is credited with (near) zero shares, so the value of her deposit accrues to existing shareholders — dominated by the attacker.

### Impact Explanation
This allows an unprivileged attacker to steal deposited principal from other unprivileged depositors in newly listed or low-liquidity banks, an in-scope core accounting/deposit path. This is a direct analog of concrete theft/insolvency impact as required.

### Likelihood Explanation
Likelihood is Low-to-Medium: the attack requires the attacker to be a dominant/sole shareholder of the target bank (achievable by front-running a newly added bank or by targeting a bank with negligible existing deposits), and requires enough capital to inflate `asset_share_value` sufficiently that a victim's expected deposit rounds to (near) zero. `I80F48` fixed-point precision (48 fractional bits) makes exact zero rounding require an extreme price ratio, but partial/near-zero minting (severe value loss, not necessarily exact zero) is achievable at much lower disparities, still resulting in material loss to the depositor.

### Recommendation
- Restrict `lending_pool_emissions_deposit` to a privileged role (e.g., group admin) rather than allowing any signer to call it, or
- Require a minimum ratio/bound between the deposited emissions `amount` and `total_asset_shares`/`total_assets` to prevent disproportionate share-price jumps in a single call, and
- Consider seeding new banks with a minimum "dead" deposit to raise the cost of becoming a dominant sole shareholder, mirroring the report's suggested mitigation of virtual shares / minimum initial deposit.

### Proof of Concept
Conceptual PoC (Rust/Anchor pseudocode against the test harness):
1. Create a new bank; have attacker deposit a minimal amount (e.g., 1 native unit) to become the sole shareholder — `total_asset_shares ≈ 1`.
2. Attacker calls `lending_pool_emissions_deposit(bank, large_amount)` where `large_amount` is chosen so that `asset_share_value = (1 + large_amount) / 1` becomes very large.
3. Victim calls the standard deposit instruction with a normal amount `N` (e.g., typical deposit size); assert `get_asset_shares(N)` (i.e., `N / asset_share_value`) rounds to a value far below `N`'s fair share, per the formula validated in `bank.rs`'s `get_asset_shares` [4](#0-3) .
4. Assert attacker's existing shares now represent a proportionally larger claim on `total_assets` (which includes the victim's undercredited deposit), confirming value transfer from victim to attacker.

Note: I could not fully verify the exact `Accounts` struct (signer/authority constraints) for `LendingPoolEmissionsDeposit` beyond the retrieved snippet and its doc comment; the index did not return that full struct definition. This should be confirmed directly in `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs` (lines preceding 68) to verify there is no unseen admin/role constraint that would invalidate this analog — I recommend starting a Devin session with full repository access to confirm this precisely if further certainty is required.

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

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L111-136)
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

**File:** programs/marginfi/src/state/bank.rs (L249-256)
```rust
    fn get_asset_shares(&self, value: I80F48) -> MarginfiResult<I80F48> {
        if self.asset_share_value == I80F48::ZERO.into() {
            return Ok(I80F48::ZERO);
        }
        Ok(value
            .checked_div(self.asset_share_value.into())
            .ok_or_else(math_error!())?)
    }
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1855-1856)
```rust
        let asset_shares_increase = bank.get_asset_shares(asset_amount_increase)?;
        balance.change_asset_shares(asset_shares_increase)?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L2036-2046)
```rust
        ) -> (Bank, Balance) {
            let mut bank = Bank::zeroed();
            bank.asset_share_value = asset_share_value.into();
            bank.liability_share_value = liability_share_value.into();
            // Buffer the totals so utilization stays healthy after the call
            // and so dust on the prohibited side, if leaked, doesn't fail the
            // utilization-ratio check.
            let buffer = I80F48::from_num(1_000);
            bank.total_asset_shares = (asset_shares + buffer).into();
            bank.total_liability_shares = liability_shares.into();
            bank.config.deposit_limit = u64::MAX;
```
