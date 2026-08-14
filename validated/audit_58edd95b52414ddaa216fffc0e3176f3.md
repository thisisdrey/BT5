### Title
Isolated-tier assets are excluded from `Equity` value calculation, allowing a user to be misclassified as bankrupt while retaining real collateral value - (File: `programs/marginfi/src/state/marginfi_account.rs`)

### Summary
`calc_weighted_asset_value_cached_standalone` and `calc_weighted_asset_value_standalone` unconditionally return `(I80F48::ZERO, I80F48::ZERO)` for any balance in a `RiskTier::Isolated` bank, regardless of `RequirementType`. Since `check_account_bankrupt` computes `equity_assets`/`equity_liabs` using `RequirementType::Equity` (which `BankConfig::get_weight` otherwise defines as weight `1.0`, i.e. full unweighted value), Isolated-tier deposits are silently excluded from the bankruptcy equity calculation even though the account genuinely still holds and controls that value.

### Finding Description
`check_account_bankrupt` in `programs/marginfi/src/state/marginfi_account.rs` (lines 964-996) computes: [1](#0-0) 

using `RequirementType::Equity` via `get_health_components`. `BankConfig::get_weight` correctly special-cases `Equity` to always return `I80F48::ONE` (full value, no discount), for both assets and liabilities: [2](#0-1) 

However, the actual per-balance valuation functions used by the health engine, `calc_weighted_asset_value_cached_standalone` and `calc_weighted_asset_value_standalone`, never call `get_weight`/`get_asset_weight` for `RiskTier::Isolated` balances — they hardcode the value to zero for *any* `RequirementType`, including `Equity`: [3](#0-2) [4](#0-3) 

This means that regardless of how much real value a user holds in an Isolated-tier deposit, it contributes `0` to `equity_assets` in the bankruptcy check. Per the project's own documentation, `BANKRUPTCY.md` states bankruptcy eligibility should be based on assets "before accounting for weights" — i.e. true, unweighted equity — but the Isolated branch never reaches even the unweighted value; it is forced to zero unconditionally.

Exploit flow (all unprivileged, reachable instructions):
1. User deposits into an Isolated-tier bank (`lending_account_deposit`) — legal, since isolated tier only restricts *borrowing*, not depositing (`check_account_risk_tiers`, `programs/marginfi/src/state/marginfi_account.rs:1040-1089`, only limits simultaneous liability positions when one is isolated).
2. User separately deposits Collateral-tier collateral and borrows against it.
3. The Collateral-tier position becomes undercollateralized (price move / interest accrual) and is liquidated down to near-zero assets by a liquidator, or the user simply lets liabilities exceed the (mis-measured) equity assets.
4. Once `equity_liabs > 0`, `equity_assets < BANKRUPT_THRESHOLD` (computed while ignoring the real value of the Isolated deposit), and `equity_liabs > equity_assets`, `is_bankrupt` returns true.
5. `lending_pool_handle_bankruptcy` (`programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs`) is invoked — permissionlessly if `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG` is set on the debt bank (documented as callable by "any signer"): [5](#0-4) 
6. The handler only looks up and repays the balance for the specific liability `bank_loader.key()` — it never touches or seizes balances in other banks: [6](#0-5) [7](#0-6) 
7. The Isolated-tier asset balance remains untouched in the account, and the account is simply disabled (`ACCOUNT_DISABLED`) with the debt wiped/socialized.

The user thus retains the Isolated-tier asset (still withdrawable in principle, though the account gets flagged `ACCOUNT_DISABLED`, which would need checking whether disabled accounts can still withdraw — but at minimum, the value is never seized or counted toward covering the bad debt) while the collateral-tier liability's shortfall is covered by insurance and/or socialized to other Collateral-tier depositors.

### Impact Explanation
This breaks the solvency invariant that bankruptcy write-downs should only apply to genuinely unbacked debt. A user can retain real, uncounted collateral (the Isolated deposit) while having the protocol/insurance fund and other depositors absorb a liability that was, in truth, partially or fully backed. This is unauthorized value retention at the expense of socialized losses — a form of insolvency/bad-debt-shifting that matches the bounty's "insolvency/bad debt" and "unauthorized state change" categories.

### Likelihood Explanation
Preconditions are attacker-controllable except for one bank-level configuration flag (`PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG`), which must be enabled on the debt bank for a non-admin to invoke `HandleBankruptcy` directly; absent that flag, the attacker would need to wait for admin/risk_admin to run the crank (still possible since the crank is expected to run routinely per the bankruptcy guide, and the mis-valuation persists regardless of who triggers it). The deposit/borrow/liquidation sequence itself uses only standard, legitimate instructions available to any depositor. The core defect — Isolated tier always valued at zero even under `RequirementType::Equity` — is deterministic and does not depend on oracle manipulation or edge-case timing.

### Recommendation
In `calc_weighted_asset_value_cached_standalone` and `calc_weighted_asset_value_standalone`, do not force `RiskTier::Isolated` assets to zero value for `RequirementType::Equity`. Only zero out the Initial/Maintenance collateral contribution (per design, isolated assets shouldn't count as *borrowing power*), but for Equity/solvency purposes the true unweighted value (using `bank.config.get_weight(Equity, Assets)` = 1.0) must be included, consistent with `BankConfig::get_weight`'s existing `Equity` special case. Alternatively, have `check_account_bankrupt`/`lending_pool_handle_bankruptcy` require that all balances (including Isolated-tier assets) are fully liquidated/seized before allowing bankruptcy processing on any liability of that account.

### Proof of Concept
Rust integration test plan (extending `programs/marginfi/tests/admin_actions/bankruptcy.rs`):
1. Create three banks: `usdc_bank` (Collateral, debt asset), `collateral_bank` (Collateral), `isolated_bank` (Isolated, `asset_weight_init = asset_weight_maint = 0`).
2. LP deposits into `usdc_bank` to provide borrowable liquidity.
3. Victim account deposits into `isolated_bank` (e.g. $1000 worth) and separately deposits into `collateral_bank` (e.g. $100), then borrows `usdc_bank` up to the limit allowed by `collateral_bank` alone.
4. Drive the `collateral_bank` position to zero via liquidation (simulate oracle drop or use `try_bank_liquidate` to fully seize the collateral-tier deposit), leaving only the Isolated deposit and the USDC liability.
5. Call `lending_account_pulse_health` or `check_account_bankrupt` directly and assert `is_bankrupt == true` even though the account's true total assets (Isolated $1000 + remaining) still exceed the liability.
6. Call `try_handle_bankruptcy` on `usdc_bank` for the victim and assert it succeeds.
7. Post-condition assertions:
   - Victim's `isolated_bank` balance is unchanged (non-zero) after bankruptcy.
   - `usdc_bank` liability shares for the victim are zeroed/repaid.
   - Insurance fund and/or LP depositors in `usdc_bank` absorbed the socialized loss (`assetShareValue` decreased or insurance vault drained), despite the victim retaining un-seized value in `isolated_bank`.
   - Assert this violates `SOLVENCY`: total protocol loss (`socialized_loss` + insurance drawn) should equal `liability - true_all_tier_collateral`, but instead equals `liability - collateral_tier_weighted_collateral_only`, i.e., strictly greater than the true bad debt by the value retained in the Isolated bank.

### Citations

**File:** programs/marginfi/src/state/marginfi_account.rs (L356-396)
```rust
#[inline(always)]
fn calc_weighted_asset_value_cached_standalone(
    balance: &Balance,
    bank: &Bank,
    requirement_type: RequirementType,
    emode_config: &EmodeConfig,
) -> MarginfiResult<(I80F48, I80F48)> {
    match bank.config.risk_tier {
        RiskTier::Collateral => {
            if matches!(
                (bank.config.operational_state, requirement_type),
                (BankOperationalState::ReduceOnly, RequirementType::Initial)
            ) {
                debug!("ReduceOnly bank assets worth 0 for Initial margin");
                return Ok((I80F48::ZERO, I80F48::ZERO));
            }

            let mut asset_weight = bank.get_asset_weight(requirement_type, emode_config);

            let price_with_confidence = get_cached_price_with_confidence(bank, requirement_type);
            let lower_price = apply_price_bias(price_with_confidence, PriceBias::Low)?;

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

            Ok((value, lower_price))
        }
        RiskTier::Isolated => Ok((I80F48::ZERO, I80F48::ZERO)),
    }
}
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L978-989)
```rust
    let (equity_assets, equity_liabs) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Equity,
        health_cache,
        HealthPriceMode::Live { liq_cache: None },
    )?;

    let has_liabilities = equity_liabs > I80F48::ZERO;
    let below_bankruptcy_threshold = equity_assets < BANKRUPT_THRESHOLD;
    let liabilities_exceed_assets = equity_liabs > equity_assets;
    let is_bankrupt = has_liabilities && below_bankruptcy_threshold && liabilities_exceed_assets;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1245-1333)
```rust
/// Calculate weighted asset value (standalone version for heap reuse).
#[inline(always)]
fn calc_weighted_asset_value_standalone(
    balance: &Balance,
    bank: &Bank,
    price_adapter_result: &MarginfiResult<OraclePriceFeedAdapter>,
    requirement_type: RequirementType,
    emode_config: &EmodeConfig,
    liq_cache: &mut Option<&mut LiquidationPriceCache>,
    position_index: usize,
) -> MarginfiResult<(I80F48, I80F48, u32)> {
    match bank.config.risk_tier {
        RiskTier::Collateral => {
            // ReduceOnly banks should not be counted as collateral for Initial checks
            if matches!(
                (bank.config.operational_state, requirement_type),
                (BankOperationalState::ReduceOnly, RequirementType::Initial)
            ) {
                debug!("ReduceOnly bank assets worth 0 for Initial margin");
                return Ok((I80F48::ZERO, I80F48::ZERO, 0));
            }

            // Extract error code if oracle failed
            let err_code = match price_adapter_result {
                Ok(_) => 0,
                Err(e) => match e {
                    anchor_lang::error::Error::AnchorError(inner) => {
                        inner.as_ref().error_code_number
                    }
                    anchor_lang::error::Error::ProgramError(inner) => {
                        match inner.as_ref().program_error {
                            ProgramError::Custom(code) => code,
                            _ => MarginfiError::InternalLogicError as u32,
                        }
                    }
                },
            };

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
        RiskTier::Isolated => Ok((I80F48::ZERO, I80F48::ZERO, 0)),
    }
}
```

**File:** type-crate/src/types/bank_config.rs (L144-160)
```rust
    pub fn get_weight(
        &self,
        requirement_type: RequirementType,
        balance_side: BalanceSide,
    ) -> I80F48 {
        match (requirement_type, balance_side) {
            (RequirementType::Initial, BalanceSide::Assets) => self.asset_weight_init.into(),
            (RequirementType::Initial, BalanceSide::Liabilities) => {
                self.liability_weight_init.into()
            }
            (RequirementType::Maintenance, BalanceSide::Assets) => self.asset_weight_maint.into(),
            (RequirementType::Maintenance, BalanceSide::Liabilities) => {
                self.liability_weight_maint.into()
            }
            (RequirementType::Equity, _) => I80F48::ONE,
        }
    }
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L56-67)
```rust
        let is_admin_or_risk_admin = signer == group.risk_admin || signer == group.admin;
        let permissionless_bad_debt_settlement =
            bank.get_flag(PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG);

        if permissionless_bad_debt_settlement {
            // if permissionless, users can bankrupt reduce-only or operational banks
            validate_bank_state(&bank, InstructionKind::FailsInPausedState)?;
        } else {
            // admin can bankrupt banks in any state
            validate_bank_state(&bank, InstructionKind::Unrestricted)?;
            check!(is_admin_or_risk_admin, MarginfiError::Unauthorized);
        }
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L109-128)
```rust
    let lending_account_balance = marginfi_account
        .lending_account
        .balances
        .iter_mut()
        .find(|balance| balance.is_active() && balance.bank_pk == bank_loader.key());

    check!(
        lending_account_balance.is_some(),
        MarginfiError::LendingAccountBalanceNotFound
    );

    let lending_account_balance = lending_account_balance.unwrap();

    let bad_debt: I80F48 =
        bank.get_liability_amount(lending_account_balance.liability_shares.into())?;

    check!(
        bad_debt > ZERO_AMOUNT_THRESHOLD,
        MarginfiError::BalanceNotBadDebt
    );
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L189-199)
```rust
    // Socialize bad debt among depositors.
    let kill_bank = bank.socialize_loss(socialized_loss)?;

    // Settle bad debt.
    // The liabilities of this account and global total liabilities are reduced by `bad_debt`
    BankAccountWrapper::find(
        &bank_loader.key(),
        &mut bank,
        &mut marginfi_account.lending_account,
    )?
    .repay(bad_debt)?;
```
