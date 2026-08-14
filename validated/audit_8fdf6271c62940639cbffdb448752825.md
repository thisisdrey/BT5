### Title
Flashloan-driven share inflation lets an attacker steal a disproportionate share of permissionless `emissions_deposit` rewards, diluting legitimate depositors - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
`lending_pool_emissions_deposit` is a permissionless instruction that transfers "same-mint emissions" straight into a bank's liquidity vault and re-prices `asset_share_value` for **all** existing depositors uniformly [1](#0-0) . Because share value is recomputed as `(total_assets + emissions_amount) / total_shares` at the instant the emissions deposit lands, any account that transiently owns a large fraction of `total_asset_shares` at that instant captures a proportionally large fraction of the emissions payout - exactly the "atomic capital inflation right before a reward-distribution event" pattern described in the Bonding.sol report, where a flashloan is used to force a favorable, permanent state change immediately before a reward-bearing snapshot, at the expense of genuine long-term participants.

### Finding Description
`lending_account_deposit` requires no Risk Engine / health check (only withdraws and borrows do) [2](#0-1) , so a user can freely deposit an arbitrarily large amount, taken from any external flash-loan source, into a marginfi bank and withdraw it again in the same transaction — `lending_account_withdraw` only fails the tx if the resulting account is unhealthy [3](#0-2) , and a full withdrawal of a position with no debt is trivially healthy.

The emissions mechanism, `lending_pool_emissions_deposit`, is explicitly permissionless and raises `asset_share_value` for the whole bank based on the deposited amount and the current `total_asset_shares` [1](#0-0) ; the underlying share-price mechanics for a bank use the same style of uniform "raise share value / shares" math seen in `accrue_interest`/`update_bank_cache` [4](#0-3) .

If a depositor `d = D/V` (from a flashloaned deposit `D`) is added right before an emissions deposit of `E`, the new share value becomes `V + E/(S+d)`. The attacker's share of the just-deposited rewards is `d/(S+d) * E`, which approaches the entire reward `E` as `D` (and thus `d`) grows large relative to the bank's pre-existing `total_asset_shares` `S`. The attacker then withdraws the whole flashloaned principal plus its inflated cut of `E`, repays the external flash loan, and keeps the difference — diluting or entirely capturing rewards meant for real depositors who held their position honestly, with zero holding-period requirement or cooldown enforced anywhere in `lending_pool_emissions_deposit`, `lending_account_deposit`, or `lending_account_withdraw`.

This is structurally the same bug class as the Bonding.sol `unwrapToken` issue: an atomic, flashloan-funded action forces a favorable outcome in a reward-distribution mechanism that was designed assuming organic, non-atomic participation, with no time-based restriction to prevent closing the loop within one transaction.

### Impact Explanation
- Legitimate long-term depositors of a bank receive a diminished (or near-zero) share of emissions/rewards distributed via `lending_pool_emissions_deposit`, even though they bore the actual duration risk and provided real liquidity.
- The attacker earns free yield with no capital cost beyond flash-loan fees, funded entirely from value diverted from other depositors — this is a direct value transfer/theft of yield rather than a purely theoretical griefing vector.
- Severity depends on the size of `E` relative to the flash-loan-obtainable liquidity for a given mint and on `S` (bank's pre-existing shares); for smaller/newer banks with low `total_asset_shares`, the effect is most severe and can approach 100% capture of a given emissions deposit.

### Likelihood Explanation
- No privileged access is required: the attacker only needs (a) access to an external flash loan of the bank's underlying asset, and (b) the ability to observe or predict when a `lending_pool_emissions_deposit` call will land (it is fully permissionless, so anyone, including a protocol/community rewards distributor, can trigger it, and the mempool/timing of such a call is generally observable/front-runnable).
- `lending_account_deposit` and `lending_account_withdraw` impose no minimum holding period and no explicit anti-flashloan/anti-sandwich guard on share-value-affecting events.
- The main friction is timing: the attacker's deposit must land before, and withdrawal after, a specific emissions deposit transaction, which is a standard sandwich/front-run pattern on Solana (same slot or adjacent, using priority fees / bundles).

### Recommendation
- Introduce a minimum holding period (or a snapshot mechanism, e.g., reward accrual proportional to time-weighted share balance rather than a spot balance) before newly deposited shares become eligible for emissions.
- Alternatively, restrict `lending_pool_emissions_deposit` to be triggerable only by an authorized emissions admin, and require it to be paired atomically (same transaction, non-frontrunnable) with the funding source, or use a streaming/vesting distribution (e.g., per-second `emissions_rate` accrual already used elsewhere in `Bank`, rather than lump-sum deposits) so that no single-block deposit can capture a disproportionate share.
- Consider adding a deposit/withdraw cooldown or a small entry/exit fee proportional to short-hold-duration to blunt the profitability of same-block deposit-reward-withdraw sandwiches.

### Proof of Concept
1. Attacker flash-loans a large amount `D` of a bank's underlying mint from an external Solana lending/DEX flash-loan source.
2. In the same transaction, attacker calls `lending_account_deposit` into the target marginfi bank with `D` (no health check required for deposits).
3. A (predictable or front-run) `lending_pool_emissions_deposit(E)` call executes, raising `asset_share_value` for the whole bank based on `total_asset_shares` that now includes the attacker's inflated temporary shares [1](#0-0) .
4. Attacker calls `lending_account_withdraw` with `withdraw_all: true`, receiving `D` plus `d/(S+d) * E` of the emissions reward; health check passes trivially since the account has zero debt [3](#0-2) .
5. Attacker repays the external flash loan (plus its fee) from the withdrawn proceeds, netting the majority of `E` that should have accrued to the bank's genuine depositors.

Note: I was unable to fully view the body of `lending_pool_emissions_deposit` (in `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`) beyond its declared behavior in `lib.rs` and the associated tests, due to index size limits — only partial file contents were retrievable through the available search tools. Starting a full Devin session would allow inspection of the complete instruction body (and any related guard checks such as minimum-share thresholds) to confirm whether any additional mitigations already exist there.

### Citations

**File:** programs/marginfi/src/lib.rs (L209-216)
```rust
    /// (permissionless) Deposit same-bank emissions directly into liquidity vault and increase
    /// depositors' value via `asset_share_value`.
    pub fn lending_pool_emissions_deposit(
        ctx: Context<LendingPoolEmissionsDeposit>,
        amount: u64,
    ) -> MarginfiResult {
        marginfi_group::lending_pool_emissions_deposit(ctx, amount)
    }
```

**File:** README.md (L273-276)
```markdown
Whenever a user Borrows or Withdraws, the Risk Engine determines if the user would be within
acceptable risk parameters after the tx completes, rejecting the tx if not. Deposits and Repays
require no Risk Engine check, as they can only improve health. Accounts requiring the Risk Engine
check must pass all Banks and Oracles involved in the user's Balances in remaining accounts.
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L219-233)
```rust
    // Note: during receivership and order execution, we skip all health checks until the end of the transaction.
    if !marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP | ACCOUNT_IN_ORDER_EXECUTION) {
        // Check account health, if below threshold fail transaction
        // Assuming `ctx.remaining_accounts` holds only oracle accounts
        // Uses heap-efficient health check to support accounts with up to 16 positions
        check_account_init_health(
            &marginfi_account,
            ctx.remaining_accounts,
            &mut Some(&mut health_cache),
        )?;
        health_cache.program_version = PROGRAM_VERSION;

        health_cache.set_engine_ok(true);
        marginfi_account.health_cache = health_cache;
    }
```

**File:** programs/marginfi/src/state/bank.rs (L625-659)
```rust
    /// Updates bank cache with the actual values for interest/fee rates.
    ///
    /// Should be called in the end of each instruction calling `accrue_interest` to ensure the cache is up to date.
    ///
    /// # Arguments
    /// * `group` - The marginfi group
    fn update_bank_cache(&mut self, group: &MarginfiGroup) -> MarginfiResult<()> {
        if self.cache.is_liquidation_price_cache_locked() {
            return Ok(());
        }
        let total_assets_amount: I80F48 = self.get_asset_amount(self.total_asset_shares.into())?;
        let total_liabilities_amount: I80F48 =
            self.get_liability_amount(self.total_liability_shares.into())?;

        if (total_assets_amount == I80F48::ZERO) || (total_liabilities_amount == I80F48::ZERO) {
            self.cache.reset_preserving_oracle_price();
            return Ok(());
        }

        let ir_calc = self
            .config
            .interest_rate_config
            .create_interest_rate_calculator(group);

        let utilization_rate: I80F48 = total_liabilities_amount
            .checked_div(total_assets_amount)
            .ok_or_else(math_error!())?;
        let interest_rates = ir_calc.calc_interest_rate(utilization_rate)?;

        update_interest_rates(&mut self.cache, &interest_rates);

        // Update banks last update timestamp
        self.last_update = Clock::get()?.unix_timestamp;
        Ok(())
    }
```
