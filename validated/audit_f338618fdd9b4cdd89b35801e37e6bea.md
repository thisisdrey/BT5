### Title
Overlapping receivership `start_liquidation` calls on a shared Bank overwrite `BankCache.liquidation_price_rt/twap` before the original `end_liquidation` consumes it - (File: `programs/marginfi/src/state/marginfi_account.rs`)

### Summary
`start_liquidation`/`start_deleverage` snapshot per-bank oracle prices into a shared, account-agnostic cache field (`Bank.cache.liquidation_price_rt`/`liquidation_price_twap`) that `end_liquidation`/`end_deleverage` later reads back via `HealthPriceMode::Cached` to determine whether the liquidation improved health and how much can be seized. This is architecturally the same class of bug as the Dandelion `newToken`/`newTokenAndBaseInstance` issue: a value is cached by one call for consumption by a second, later call, but the cache slot is keyed by a shared resource (the `Bank`, analogous to `msg.sender`'s single token slot) rather than by the specific receivership session, so an unrelated/interleaved call that touches the same resource can silently overwrite the cached value before it is consumed.

### Finding Description
`start_receivership` (shared logic for `start_liquidation` and `start_deleverage`) computes live oracle prices and, via `write_liquidation_price_cache_from`, writes them directly into each touched `Bank`'s cache and sets the `LIQ_CACHE_LOCKED_FLAG`: [1](#0-0) [2](#0-1) 

The lock is only read to short-circuit unrelated cache updates (`update_bank_cache`/`update_cache_price` in `bank.rs`) and is checked at read time in `end_receivership`, but `write_liquidation_price_cache_from` itself never checks whether the lock is *already* set before overwriting `liquidation_price_rt`/`liquidation_price_twap`: [3](#0-2) [4](#0-3) 

The cache is keyed purely by `Bank`, not by which account/receivership session wrote it. If a second, independent `start_liquidation` (for a different marginfi account) touches the same bank while a first liquidation's `start`→...→`end` window is still open, its write overwrites the price the first session cached — the lock flag stays "set" throughout (idempotent), so `end_liquidation`'s lock-presence check in `end_receivership` still passes, but the actual price values are now the second session's, not the first's: [5](#0-4) 

This mirrors the reported bug class exactly: two logically-separate operations (`newToken`/`newBaseInstance` there, two independent `start_liquidation` calls here) share one overwriteable cache slot (`deployedContracts[msg.sender].token` there, `Bank.cache.liquidation_price*` here), and a later consumer (`installDandelionApps`/`_getToken` there, `end_liquidation`'s `HealthPriceMode::Cached` read here) trusts whatever is currently cached without verifying it belongs to the same logical session.

### Impact Explanation
If exploitable, a liquidator could interleave a second `start_liquidation` on a shared bank (e.g., depositing/borrowing to make a throwaway account momentarily liquidatable, or targeting a bank shared with another in-flight liquidation) to overwrite the cached price used by `end_liquidation`'s health/seizure-limit check. Because `LiquidationPremiumTooHigh` and "health must not get worse" checks in `end_receivership` rely on this cached price via `HealthPriceMode::Cached`, a manipulated (stale/favorable) price could let a liquidator seize more collateral than the 2.5%/2.5% bonus intends, causing insolvency/bad debt for the protocol — a concrete "theft/insolvency" impact class.

### Likelihood Explanation
This is **not confirmed exploitable** with the available read-only access. Key open questions that could not be resolved in this pass because tool budget ran out:
- Whether the top-level instruction-sequence validation in `liquidate_start.rs` (`validate_ix_first`/`validate_ix_last`/`validate_ixes_exclusive`, seen imported but not fully read) forbids more than one `start_liquidation`/`start_deleverage` pair per transaction, which would block same-tx overwrite.
- Whether cross-transaction interleaving is possible (i.e., can account A's receivership remain open across multiple transactions, allowing an attacker to slip in account B's `start_liquidation` on the same bank in between)? The `ACCOUNT_IN_RECEIVERSHIP` flag is per-account, and nothing found so far shows a per-*bank* guard preventing a second `start_liquidation` (for a different account) while the bank's `liq_cache_locked` flag is already set from a different session.
- The `flashloan`/CPI restrictions (`validate_not_cpi_by_stack_height`, the `ForbiddenIx` test in `liquidate_receiver_cpi.rs`) suggest the team has already hardened against several interleaving tricks for the receivership flow, so this specific overwrite path may already be closed by an instruction-ordering constraint not fully verified here.

Given this uncertainty, I cannot assert this is a proven, currently-exploitable vulnerability — only that the caching pattern is structurally analogous to the reported bug class and warrants targeted verification.

### Recommendation
- Verify whether `start_receivership`/`write_liquidation_price_cache_from` should refuse to write (or should treat it as an error) when `Bank.cache.is_liquidation_price_cache_locked()` is already `true`, similar to how `newToken`/`newBaseInstance` were made unreachable independently in the referenced fix.
- Consider keying/validating the liquidation price cache to the specific `liquidation_record`/receivership session (e.g., store the receiver/account pubkey alongside the cached price, or require exclusivity per bank across concurrent receivership sessions) rather than trusting "the lock bit is set" as proof the cached value belongs to the currently-active session.
- Confirm and, if necessary, enforce (via `validate_instructions`/ix-sysvar checks) that no other `start_liquidation`/`start_deleverage` touching an overlapping set of banks can execute while an account is already `ACCOUNT_IN_RECEIVERSHIP`.

### Proof of Concept
Not constructed — a concrete PoC would require confirming (with terminal/test execution access not available in this read-only session) whether the transaction/instruction-ordering validators in `liquidate_start.rs` currently permit two independent `start_liquidation` instructions in one transaction (or across transactions with account A left "open") against a shared bank. A background Devin session with terminal access should attempt: (1) open `start_liquidation` for account A on `sol_bank`, (2) within the same tx (or before A's `end_liquidation` lands), execute `start_liquidation` for unrelated account B also touching `sol_bank`, (3) then run A's `end_liquidation` and check whether it consumes B's (attacker-favorable) cached price rather than A's.

### Citations

**File:** programs/marginfi/src/state/marginfi_account.rs (L295-319)
```rust
    fn write_liquidation_price_cache_from(
        &self,
        liq_cache: &LiquidationPriceCache,
        index: usize,
    ) -> MarginfiResult<()> {
        let mut bank = self.bank.load_mut()?;
        let zero_price = OraclePriceWithConfidence {
            price: I80F48::ZERO,
            confidence: I80F48::ZERO,
        };
        let price_rt = liq_cache
            .get_price(OraclePriceType::RealTime, index)
            .unwrap_or(zero_price);
        let price_twap = liq_cache
            .get_price(OraclePriceType::TimeWeighted, index)
            .unwrap_or(zero_price);

        bank.cache.liquidation_price_rt = price_rt.price.into();
        bank.cache.liquidation_price_rt_confidence = price_rt.confidence.into();
        bank.cache.liquidation_price_twap = price_twap.price.into();
        bank.cache.liquidation_price_twap_confidence = price_twap.confidence.into();
        bank.cache.set_liquidation_price_cache_locked();

        Ok(())
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate_start.rs (L85-129)
```rust
pub fn start_receivership<'info>(
    marginfi_account: &mut MarginfiAccount,
    liq_record: &mut LiquidationRecord,
    remaining_ais: &'info [AccountInfo<'info>],
    ignore_healthy: bool,
) -> MarginfiResult {
    // Note: the receiver can use the health cache state after this ix concludes to plan their
    // liquidation/deleverage strategy.
    let mut health_cache = HealthCache::zeroed();
    let mut liq_price_cache = LiquidationPriceCache::default();
    let (_pre_health, assets, liabs) = check_pre_liquidation_condition_and_get_account_health(
        marginfi_account,
        remaining_ais,
        None,
        &mut Some(&mut health_cache),
        HealthPriceMode::Live {
            liq_cache: Some(&mut liq_price_cache),
        },
        ignore_healthy,
    )?;

    // Use heap-efficient equity calculation
    let (assets_equity, liabs_equity) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Equity,
        &mut Some(&mut health_cache),
        HealthPriceMode::Live {
            liq_cache: Some(&mut liq_price_cache),
        },
    )?;

    write_liquidation_price_cache_from(marginfi_account, remaining_ais, &liq_price_cache)?;
    marginfi_account.health_cache = health_cache;
    marginfi_account.set_flag(ACCOUNT_IN_RECEIVERSHIP, false);
    marginfi_account.indexer_flags.has_ever_been_liquidated = 1;

    // Snapshot values to use in later checks
    liq_record.cache.asset_value_maint = assets.into();
    liq_record.cache.liability_value_maint = liabs.into();
    liq_record.cache.asset_value_equity = assets_equity.into();
    liq_record.cache.liability_value_equity = liabs_equity.into();

    Ok(())
}
```

**File:** programs/marginfi/src/state/bank.rs (L631-634)
```rust
    fn update_bank_cache(&mut self, group: &MarginfiGroup) -> MarginfiResult<()> {
        if self.cache.is_liquidation_price_cache_locked() {
            return Ok(());
        }
```

**File:** type-crate/src/types/bank_cache.rs (L55-64)
```rust
    /// Liquidation cache flags, set during receivership flow.
    /// * 1 (LIQ_CACHE_LOCKED_FLAG) - We "lock" the liquidation cache when writing to it in Start
    ///   Liquidate as an additional safeguard, if the liquidation prices stored here were to be
    ///   edited between start and end, it would completely break the risk engine. End validates that
    ///   the lock is set, panics if not, and removes it - which prevents footguns if the cache was
    ///   e.g. accidently set to default. The lock is also removed when a Balance is closed via
    ///   withdraw_all, repay_all, or close_balance, but only when the account has
    ///   ACCOUNT_IN_RECEIVERSHIP set, so that operations on unrelated accounts sharing the same
    ///   bank do not interfere with an in-progress liquidation.
    pub liq_cache_flags: u8,
```

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate_end.rs (L111-153)
```rust
// Common logic for both liquidation and deleverage
pub fn end_receivership<'info>(
    marginfi_account: &mut MarginfiAccount,
    liq_record: &mut LiquidationRecord,
    remaining_ais: &'info [AccountInfo<'info>],
    ignore_healthy: bool,
) -> Result<(I80F48, f64, I80F48, f64)> {
    let pre_assets: I80F48 = liq_record.cache.asset_value_maint.into();
    let pre_liabs: I80F48 = liq_record.cache.liability_value_maint.into();
    let pre_assets_equity: I80F48 = liq_record.cache.asset_value_equity.into();
    let pre_liabs_equity: I80F48 = liq_record.cache.liability_value_equity.into();
    let pre_health: I80F48 = pre_assets - pre_liabs;

    let mut post_hc = HealthCache::zeroed();
    let (post_health, _post_assets, _post_liabs) =
        check_pre_liquidation_condition_and_get_account_health(
            marginfi_account,
            remaining_ais,
            None,
            &mut Some(&mut post_hc),
            HealthPriceMode::Cached,
            ignore_healthy,
        )?;
    let (post_assets_equity, post_liabilities_equity) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Equity,
        &mut Some(&mut post_hc),
        HealthPriceMode::Cached,
    )?;

    clear_liquidation_price_cache_locks(marginfi_account, remaining_ais)?;
    marginfi_account.health_cache = post_hc;

    // health must not get worse
    if pre_health > post_health {
        msg!(
            "pre_health > post_health: {} >= {}",
            pre_health,
            post_health
        );
        return err!(MarginfiError::WorseHealthPostLiquidation);
    }
```
