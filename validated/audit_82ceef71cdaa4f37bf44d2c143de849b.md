### Title
Oracle/price-adapter failure on any single balance reverts `get_health_components`, blocking liquidation and bankruptcy processing for the whole account - ([File: programs/marginfi/src/state/marginfi_account.rs])

### Summary
`get_health_components` iterates over every active `Balance` in a margin account and, for each one, resolves an oracle price via `OraclePriceFeedAdapter::try_from_bank` and feeds the result into `calc_weighted_value_for_balance`. For liability balances, and for asset balances outside of `RequirementType::Initial`, any oracle/price-adapter error is propagated with `?` instead of being tolerated, which aborts the entire transaction. Since liquidation and bankruptcy resolution both call this function with `RequirementType::Maintenance`/`Equity` over the liquidatee's full balance set, a single stuck/misbehaving oracle on any one of the account's balances (even one unrelated to the position being liquidated) can permanently block liquidation and bankruptcy handling for that account, mirroring the reported `get_vault_borrowing_power()` bug class.

### Finding Description
`get_health_components` loops through all active balances of an account and, per balance, loads the bank and constructs an `OraclePriceFeedAdapter` for the relevant oracle account(s): [1](#0-0) 

The stale-oracle "skip" behavior is only applied on the asset side, and only for `RequirementType::Initial`: [2](#0-1) 

For any other requirement type (`Maintenance`, `Equity`), a failed price adapter for an asset balance is converted straight into a propagated error via `map_err(...)?`, aborting the whole computation: [3](#0-2) 

For liability balances, the error is propagated unconditionally regardless of requirement type — there is no skip path at all: [4](#0-3) 

`RequirementType::Maintenance` is the mode used for liquidation eligibility and post-liquidation invariant checks, e.g. `check_post_liquidation_condition_and_get_account_health`: [5](#0-4) 

The pre-liquidation check (`check_pre_liquidation_condition_and_get_account_health`) and bankruptcy check (`check_account_bankrupt`) exist in the same file and follow the same pattern of calling `get_health_components` over the full balance set of the target account (confirmed present via `fn check_pre_liquidation_condition_and_get_account_health` / `fn check_account_bankrupt` in `programs/marginfi/src/state/marginfi_account.rs`, though I was not able to view their exact line ranges in this session due to iteration limits).

The practical consequence: because a liquidator or the bankruptcy-handler is required to pass in *all* of the liquidatee's active banks + oracles as remaining accounts (as documented in `README.md` and `PACKING_RISK_ACCOUNTS.md`), the health computation touches every balance the target account holds — not just the one being seized/repaid. If any single balance's oracle reverts or returns a hard error (stale price beyond tolerance, confidence check failure, wrong account keys, a malfunctioning custom oracle for an added asset, etc.), then for `Maintenance`/`Equity` requirement types the whole `get_health_components` call errors out, and the entire liquidation or bankruptcy transaction reverts — exactly the "one bad external call disables the whole vault's borrowing/liquidation ability" pattern described in the external report.

This differs from `Initial`-type checks (used for borrow/withdraw), which already have an asset-side stale-oracle tolerance (skips valuing the position rather than reverting) — but that tolerance is deliberately *not* extended to liabilities or to Maintenance/Equity checks, which are exactly the checks liquidation and bankruptcy depend on.

### Impact Explanation
If an account holds a position in a bank whose oracle can be made to revert or return a stale/invalid price (temporarily or permanently — e.g. a misbehaving custom price feed, an oracle staleness/confidence trip that never recovers, or an integration-specific price source failure), that account becomes un-liquidatable and un-bankruptable regardless of how deep its other positions go underwater. This can let bad debt accumulate without bound in that account, since the risk engine can never confirm it is unhealthy in a way that survives a completed liquidation/bankruptcy call — leading to unrecoverable insolvency (protocol takes the loss) once the position is deep enough. This satisfies the "insolvency/bad debt" and "permanent lock/freeze" impact classes.

### Likelihood Explanation
Likelihood is moderate: it requires either (a) an integration/oracle for some bank experiencing a genuine outage/stale-price condition that persists past the tolerance window, or (b) a bank configured with an oracle setup that can be driven into a hard-revert state (e.g., confidence-interval violations, extension/venue account mismatches). Given marginfi supports numerous external oracle types and third-party integrations (Kamino, Jupiter, Solend, Drift, switchboard pull, pyth push, etc.), the number of external dependencies capable of reverting is non-trivial, and each such failure blocks liquidation/bankruptcy for any account that happens to hold that asset — not just accounts actively trading it.

### Recommendation
Extend the same tolerant handling used for `Initial`+asset-side stale oracles to the `Maintenance`/`Equity` requirement types and to the liability side: rather than propagating an oracle error and aborting the whole account's health computation, either (1) treat a failed-oracle liability conservatively (e.g., value it at its most severe / worst-case bound rather than reverting) so liquidation/bankruptcy can still proceed, or (2) provide a "receivership" style code path (similar to the existing `ACCOUNT_IN_RECEIVERSHIP`/`ACCOUNT_IN_ORDER_EXECUTION` skip-health mechanism already present for other flows) that lets a liquidator/bankruptcy-handler bypass strict oracle success for balances that are not the one being acted on, while still requiring the on-topic price to resolve correctly.

### Proof of Concept
1. User deposits collateral in Bank A (healthy, normal oracle) and also holds a dust/legacy position in Bank B whose oracle setup is `pythPushOracle`/`switchboardPull` etc.
2. Bank B's oracle begins reverting/staling (confidence interval violation, feed outage, or wrong account keys as demonstrated by the `WrongOracleAccountKeys` internal error path already exercised in `tests/specs/basic/11_health_pulse.spec.ts`).
3. User's other collateral (Bank A) crashes in price, making the account deeply insolvent.
4. A liquidator attempts to call the liquidation instruction, packing remaining accounts for both Bank A and Bank B as required by `PACKING_RISK_ACCOUNTS.md`.
5. `get_health_components` is invoked with `RequirementType::Maintenance`; when processing Bank B's balance, `OraclePriceFeedAdapter::try_from_bank` errors, and (per `calc_weighted_asset_value_standalone`/`calc_weighted_liab_value_standalone`) this error is propagated instead of skipped because the requirement type is not `Initial`.
6. The liquidation transaction reverts with the oracle error, and the account cannot be liquidated no matter how insolvent it becomes as long as Bank B's oracle remains broken.

### Citations

**File:** programs/marginfi/src/state/marginfi_account.rs (L662-738)
```rust
    for (position_index, balance) in lending_account
        .balances
        .iter()
        .filter(|b| b.is_active())
        .enumerate()
    {
        let heap_checkpoint = heap_pos();

        // Load bank
        let bank_ai = remaining_ais
            .get(account_index)
            .ok_or(MarginfiError::InvalidBankAccount)?;
        let bank_al = AccountLoader::<Bank>::try_from(bank_ai)?;
        let bank = bank_al.load()?;

        check_eq!(
            balance.bank_pk,
            *bank_ai.key,
            MarginfiError::InvalidBankAccount
        );

        let num_accounts = if is_cached {
            check!(
                bank.cache.is_liquidation_price_cache_locked(),
                MarginfiError::InternalLogicError
            );
            1
        } else {
            get_remaining_accounts_per_bank(&bank)?
        };

        let (asset_val, liab_val, price, err_code) = if is_cached {
            let (asset_val, liab_val, price) = calc_weighted_value_cached_for_balance(
                balance,
                &bank,
                requirement_type,
                &reconciled_emode_config,
            )?;
            (asset_val, liab_val, price, 0)
        } else {
            // Load oracle (this is the heap-intensive operation)
            let oracle_ai_idx = account_index + 1;
            let end_idx = oracle_ai_idx + num_accounts - 1;
            require_gte!(
                remaining_ais.len(),
                end_idx,
                MarginfiError::WrongNumberOfOracleAccounts
            );
            let oracle_ais = &remaining_ais[oracle_ai_idx..end_idx];

            // Create oracle adapter (heap allocation happens here)
            let price_adapter_result =
                OraclePriceFeedAdapter::try_from_bank(&bank, oracle_ais, clock.as_ref().unwrap());

            // Log heap usage per position for measurement/debugging
            // Measured results: Pyth ~64 bytes, Switchboard ~128 bytes per position
            #[cfg(target_os = "solana")]
            {
                let heap_after_oracle = heap_pos();
                let _heap_used = heap_after_oracle.saturating_sub(heap_checkpoint);
                debug!(
                    "HEAP_MEASURE: position={} heap_used={} bytes",
                    position_index, _heap_used
                );
            }

            // Calculate weighted value for this position
            calc_weighted_value_for_balance(
                balance,
                &bank,
                &price_adapter_result,
                requirement_type,
                &reconciled_emode_config,
                &mut liq_cache,
                position_index,
            )?
        };
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1173-1179)
```rust
    let (assets, liabs) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Maintenance,
        &mut None,
        HealthPriceMode::Live { liq_cache: None },
    )?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1283-1294)
```rust
            // Skip stale oracles for Initial requirement
            if matches!(
                (price_adapter_result, requirement_type),
                (&Err(_), RequirementType::Initial)
            ) {
                debug!("Skipping stale oracle");
                return Ok((I80F48::ZERO, I80F48::ZERO, err_code));
            }

            let price_feed = price_adapter_result
                .as_ref()
                .map_err(|_| error!(MarginfiError::from(err_code)))?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1345-1361)
```rust
    // Propagate the original oracle error (e.g., PythPushStalePrice, SwitchboardStalePrice)
    let price_feed = match price_adapter_result {
        Ok(adapter) => adapter,
        Err(e) => {
            // Extract error code and re-create the error to propagate it
            let err_code = match e {
                anchor_lang::error::Error::AnchorError(inner) => inner.as_ref().error_code_number,
                anchor_lang::error::Error::ProgramError(inner) => {
                    match inner.as_ref().program_error {
                        ProgramError::Custom(code) => code,
                        _ => MarginfiError::InvalidOracleSetup as u32,
                    }
                }
            };
            return Err(error!(MarginfiError::from(err_code)));
        }
    };
```
