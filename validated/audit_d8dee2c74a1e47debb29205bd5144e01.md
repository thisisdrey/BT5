### Title
Zero LST supply in `StakedWithPythPush` oracle pricing permanently bricks all positions in the affected staked-collateral bank - (File: `programs/marginfi/src/state/price.rs`)

### Summary
The Notional report shows that `getWithdrawRequestValue()` reverts unconditionally when an assumed-nonzero value (`exitBalance`) actually reaches zero, and because `price()` depends on this function, every downstream operation (withdraw, repay, liquidate) that needs the account's price becomes permanently blocked. marginfi-v2 has the same structural pattern in `OraclePriceFeedAdapter::load_oracle_context_with_max_age` for `OracleSetup::StakedWithPythPush`: it hard-reverts with `MarginfiError::ZeroSupplyInStakePool` if the LST mint's `supply` is ever zero, and this pricing function is the single choke point used by every health-check, withdraw, repay, liquidation, and bankruptcy code path for balances in that bank.

### Finding Description
In `try_from_bank` → `load_oracle_context_with_max_age`, the `StakedWithPythPush` branch reads the LST mint supply and checks: [1](#0-0) 
```
let lst_mint = Account::<'info, Mint>::try_from(&ais[1]).unwrap();
let lst_supply = lst_mint.supply;
check!(lst_supply > 0, MarginfiError::ZeroSupplyInStakePool);
```
If `lst_supply` is `0`, this returns a hard `Err`, propagating out of `try_from_bank`. This same oracle-loading function is invoked from `get_health_components`, which is the single core routine used by: [2](#0-1) 
```
let price_adapter_result =
    OraclePriceFeedAdapter::try_from_bank(&bank, oracle_ais, clock.as_ref().unwrap());
```
`get_health_components` backs `check_account_init_health` (borrow/withdraw), `check_pre_liquidation_condition_and_get_account_health` (liquidation), and bankruptcy checks: [3](#0-2) [4](#0-3) 

So the assumption baked into the pricing code is that the SVSP/single-validator-pool LST mint's total supply will never reach zero. This is the same class of flaw as the Notional bug: an edge condition (a value hitting exactly zero) that is not actually prevented by any invariant in the system, but which the price-dependent critical path assumes can never occur, causes a hard revert instead of graceful degradation. Because `lst_supply` is the *entire mint's* circulating supply (not scoped to marginfi depositors), if the underlying single-validator stake pool is fully unwound by all its holders (marginfi and non-marginfi) — e.g., a deprecated/thin validator pool where all participants (including any marginfi depositors) redeem everything — `lst_supply` can legitimately drop to zero while a marginfi bank for that mint may still exist with residual dust/rounding shares, or before all balances are formally closed.

Once `lst_supply == 0`, every account holding a balance in that staked bank permanently fails price computation: withdraws, repayments, liquidations, bankruptcy resolution, and even `lending_account_pulse_health` all route through `try_from_bank` for that bank and will revert with `ZeroSupplyInStakePool`. Unlike Solend's `solend_price_multiplier`, which similarly reverts on a supposedly "unreachable" zero-collateral state: [5](#0-4) 
the staked-collateral path has no fallback and directly blocks the risk engine for the whole bank.

### Impact Explanation
If `lst_supply` reaches zero while any marginfi account still has an active balance in the corresponding staked bank, that bank becomes permanently unpriceable. Every account with a position there loses the ability to withdraw collateral, repay debt, get liquidated, or be resolved via bankruptcy — a permanent lock/freeze of funds, matching the impact bar (permanent lock/freeze) validated in this analysis. Given SVSP supports many independent single-validator pools/mints, a thin or deprecated validator's pool draining to zero supply is a realistic, unprivileged-triggerable scenario (any external redeemer of that LST, not just marginfi users, can drive supply to zero).

### Likelihood Explanation
Likelihood is moderate: it requires the *entire* LST mint supply for a given single-validator stake pool to be redeemed to zero, which is more plausible for small/inactive/deprecated validator pools than for large, actively used ones. It does not require any privileged action — it can arise purely from ordinary user behavior across the whole stake-pool ecosystem (not limited to marginfi depositors), which is consistent with the "unprivileged-user analog" scope.

### Recommendation
Avoid an unconditional hard revert on `lst_supply == 0` in the price path. Instead, either (a) prevent this state by enforcing that a marginfi staked bank cannot exist / cannot retain balances once the underlying LST supply could plausibly reach zero, or (b) allow `get_health_components` and the withdraw/repay/liquidation/bankruptcy code paths to treat a zero-supply staked oracle as a degraded/zero-valued price (similar to how missing withdraw-request values should be handled in the analogous Notional case) rather than reverting, so that positions can still be exited, liquidated, or written off instead of being frozen indefinitely.

### Proof of Concept
1. Create a `StakedWithPythPush` bank backed by an SVSP/single-validator stake pool with a small LST mint supply, and have a marginfi account deposit into it.
2. Have all other holders (and, if leaving any dust, the marginfi depositor) of that LST mint redeem/burn their tokens until `lst_mint.supply == 0`.
3. Attempt any operation touching that bank's price for the affected marginfi account — `lending_account_withdraw`, `lending_account_repay`, liquidation, bankruptcy, or `lending_account_pulse_health` — each will call `OraclePriceFeedAdapter::try_from_bank`, hit `check!(lst_supply > 0, MarginfiError::ZeroSupplyInStakePool)`, and revert, leaving the account permanently unable to exit or be resolved.

### Citations

**File:** programs/marginfi/src/state/price.rs (L259-267)
```rust
fn solend_price_multiplier(reserve: &SolendMinimalReserve) -> MarginfiResult<I80F48> {
    let (total_liq, total_col) = reserve.scaled_supplies()?;
    if total_col > I80F48::ZERO {
        Ok(total_liq / total_col)
    } else {
        // Note: expected to be unreachable
        Err(MarginfiError::MathError.into())
    }
}
```

**File:** programs/marginfi/src/state/price.rs (L357-359)
```rust
                let lst_mint = Account::<'info, Mint>::try_from(&ais[1]).unwrap();
                let lst_supply = lst_mint.supply;
                check!(lst_supply > 0, MarginfiError::ZeroSupplyInStakePool);
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L712-714)
```rust
            // Create oracle adapter (heap allocation happens here)
            let price_adapter_result =
                OraclePriceFeedAdapter::try_from_bank(&bank, oracle_ais, clock.as_ref().unwrap());
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L936-942)
```rust
    let (assets, liabs) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Maintenance,
        health_cache,
        price_mode,
    )?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1110-1126)
```rust
pub fn check_account_init_health<'info>(
    marginfi_account: &MarginfiAccount,
    remaining_ais: &'info [AccountInfo<'info>],
    health_cache: &mut Option<&mut HealthCache>,
) -> MarginfiResult {
    if marginfi_account.get_flag(ACCOUNT_IN_FLASHLOAN) {
        // Risk checks are skipped during flashloans
        return Ok(());
    }

    let (assets, liabs) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Initial,
        health_cache,
        HealthPriceMode::Live { liq_cache: None },
    )?;
```
