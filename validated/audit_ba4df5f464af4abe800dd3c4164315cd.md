**No vulnerability found for this question.**

Marginfi's oracle price loaders do not implement the Reserve Protocol pattern of returning a sentinel `(0, FIX_MAX)` price on staleness that then gets fed into a sizing calculation. Instead, every oracle adapter (`SwitchboardPullPriceFeed::load_checked`, `PythPushOraclePriceFeed::load_checked`) explicitly `err!`s / reverts on stale prices [1](#0-0) [2](#0-1) , rather than silently substituting an extreme price bound.

In the risk-engine value calculations, an oracle error is either propagated (liabilities, always) or the position is zeroed for Initial-requirement collateral checks — there is no code path where a stale/timed-out oracle causes a `0` price to be treated as a valid low price used to size a sell/liquidation amount [3](#0-2) [4](#0-3) .

In liquidation, the asset and liability prices are fetched from the oracle adapters and explicitly checked to be greater than zero before being used to compute liquidation amounts, reverting with `ZeroAssetPrice`/`ZeroLiabilityPrice` if not [5](#0-4) . There is no "sell all available collateral/RSR at whatever price the oracle returns" sizing logic analogous to `RecollateralizationLibP1.nextTradePair`; marginfi has no auction/rebalance subsystem that determines a sell amount from an unclamped oracle price such that a stale oracle could cause a full-balance sell-off at zero.

Since marginfi reverts on stale oracles rather than returning a degenerate low/high price bound that flows into a sizing calculation, the described bug class (unbounded sell-off at zero price due to oracle timeout fallback value) has no reachable analog in this codebase.

### Citations

**File:** programs/marginfi/src/state/price.rs (L1366-1370)
```rust
        // Check staleness
        let last_updated = feed.last_update_timestamp;
        if current_timestamp.saturating_sub(last_updated) > max_age as i64 {
            return err!(MarginfiError::SwitchboardStalePrice);
        }
```

**File:** programs/marginfi/src/state/price.rs (L1547-1562)
```rust
    pub fn load_checked(ai: &AccountInfo, clock: &Clock, max_age: u64) -> MarginfiResult<Self> {
        let price_feed_account = load_price_update_v2_checked(ai)?;
        let feed_id = &price_feed_account.price_message.feed_id;

        let price = price_feed_account
            .get_price_no_older_than_with_custom_verification_level(
                clock,
                max_age,
                feed_id,
                MIN_PYTH_PUSH_VERIFICATION_LEVEL,
            )
            .map_err(|e| {
                debug!("Pyth push oracle error: {:?}", e);
                let error: MarginfiError = e.into();
                error
            })?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1283-1330)
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

            let mut asset_weight = bank.get_asset_weight(requirement_type, emode_config);

            let lower_price = if let Some(cache) = liq_cache.as_mut() {
                let price_with_confidence = price_feed.get_price_and_confidence_of_type(
                    requirement_type.get_oracle_price_type(),
                    bank.config.oracle_max_confidence,
                )?;
                cache.record(requirement_type, position_index, price_with_confidence);
                apply_price_bias(price_with_confidence, PriceBias::Low)?
            } else {
                price_feed.get_price_of_type(
                    requirement_type.get_oracle_price_type(),
                    Some(PriceBias::Low),
                    bank.config.oracle_max_confidence,
                )?
            };

            // Apply initial discount if applicable
            if matches!(requirement_type, RequirementType::Initial) {
                if let Some(discount) = bank.maybe_get_asset_weight_init_discount(lower_price)? {
                    asset_weight = asset_weight
                        .checked_mul(discount)
                        .ok_or_else(math_error!())?;
                }
            }

            let value = calc_value(
                bank.get_asset_amount(balance.asset_shares.into())?,
                lower_price,
                bank.get_balance_decimals(),
                Some(asset_weight),
            )?;

            Ok((value, lower_price, 0))
        }
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

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate.rs (L215-235)
```rust
        let asset_price: I80F48 = fetch_asset_price_for_bank_low_bias(
            &asset_bank_key,
            &asset_bank,
            &clock,
            ctx.remaining_accounts,
        )?;
        check!(asset_price > I80F48::ZERO, MarginfiError::ZeroAssetPrice);

        let mut liab_bank = ctx.accounts.liab_bank.load_mut()?;
        let liab_bank_remaining_accounts_len = get_remaining_accounts_per_bank(&liab_bank)? - 1;
        let liab_price: I80F48 = {
            let oracle_ais = &ctx.remaining_accounts[asset_bank_remaining_accounts_len
                ..(asset_bank_remaining_accounts_len + liab_bank_remaining_accounts_len)];
            let liab_pf = OraclePriceFeedAdapter::try_from_bank(&liab_bank, oracle_ais, &clock)?;
            liab_pf.get_price_of_type(
                OraclePriceType::RealTime,
                Some(PriceBias::High),
                liab_bank.config.oracle_max_confidence,
            )?
        };
        check!(liab_price > I80F48::ZERO, MarginfiError::ZeroLiabilityPrice);
```
