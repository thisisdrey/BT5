### Title
Receivership liquidation bypasses the insurance-fund fee charged by classic liquidation, allowing liquidators to consistently avoid the socialized-loss fee - (File: `programs/marginfi/src/instructions/marginfi_account/liquidate_end.rs`)

### Summary
Marginfi has two independent liquidation mechanisms that are meant to be economically interchangeable ways of resolving the same unhealthy-account condition: "classic" liquidation (`lending_account_liquidate`) and "receivership" liquidation (`start_liquidation` / `end_liquidation`). Classic liquidation always deducts an explicit `LIQUIDATION_INSURANCE_FEE` (2.5%) that is transferred into the bank's insurance vault [1](#0-0) , and that fee is physically moved to `bank_insurance_vault` via `withdraw_spl_transfer` [2](#0-1) . Receivership liquidation, which achieves the exact same outcome (an unhealthy account's assets are seized and its liabilities repaid), charges only a nominal flat SOL fee to the global fee wallet and enforces a liquidator profit cap — but it never charges or transfers any equivalent fee into the bank's insurance vault [3](#0-2) .

### Finding Description
This mirrors the reported bug class: two logically equivalent execution paths for the same economic action (liquidating an unhealthy position) exist, but only one of them charges the fee that funds the protocol's loss-absorption mechanism (in Size, the "swap fee"; in marginfi, the "insurance fee"). `end_receivership` computes `seized` and `repaid` purely from equity-value deltas and never routes any portion of the repaid liability into `collected_insurance_fees_outstanding` or the insurance vault [4](#0-3) . The only monetary transfer to the protocol in this path is `liquidation_flat_sol_fee`, a fixed lamport amount unrelated to the size of the liquidation [5](#0-4) . By contrast, classic liquidation always skims 2.5% of the repaid liability value into the insurance fund regardless of size [1](#0-0) .

The project's own documentation confirms this asymmetry is real and describes the two fee structures as parallel but different: classic liquidation splits 2.5%/2.5% between liquidator and insurance fund, while receivership liquidation only has a "flat SOL fee" and a liquidator profit cap, with no insurance-fund contribution [6](#0-5) . `guides/ADMIN/COLLECTING_FEES.md` similarly enumerates "Legacy Liquidation fee" (2.5% to insurance) separately from "Liquidation flat sol fee" and "Receivership liquidation fee," with no insurance component listed for receivership at all [7](#0-6) .

### Impact Explanation
Because any liquidator can freely choose which of the two mechanisms to use for the same unhealthy account, a rational liquidator will always prefer receivership liquidation: it offers a higher max profit (up to 10% vs 2.5%) and does not divert any value to the insurance fund, meaning the liquidator captures the full discount that would otherwise be split with the fund [8](#0-7) . Over time this systematically starves the insurance fund of the fee income it is supposed to earn from every liquidation event, which weakens the fund's ability to cover socialized bad debt during a real bankruptcy event, since `collected_insurance_fees_outstanding` (the fund's revenue source) is never incremented on this path [9](#0-8) . This is not merely a documentation gap — it is a structural revenue leak in a core accounting/liquidation path, analogous to the reported issue: the protocol accepted this design tradeoff for one liquidation mechanism ("Receivership liquidation fee...historically no more than 10%...the liquidator collects a profit") without imposing an equivalent insurance charge, unlike the classic liquidation fee model.

### Likelihood Explanation
Likelihood is high: this is not a rare edge case but the default, encouraged behavior. The receivership guide explicitly recommends receivership liquidation over classic ("Try Receivership Liquidation instead: the process is simpler, and the potential premium paid is higher") [10](#0-9) , meaning essentially all liquidation volume can be expected to route through the path that bypasses the insurance fee, with no special privilege required — any unprivileged liquidator can call `start_liquidation`/`end_liquidation`.

### Recommendation
If parity with classic liquidation's insurance-fee mechanism is intended, charge an equivalent proportional insurance fee in `end_receivership` (or `end_liquidation`) based on the `repaid` liability equity value, transferring it into the relevant bank(s)' insurance vault(s) analogous to the SPL transfer performed in classic liquidation. If instead this asymmetry is an intentional incentive to route liquidation volume through the receivership mechanism (similar to the Size team's stated rationale for `LiquidateWithReplacement`), this should be explicitly documented as a deliberate economic/product tradeoff rather than left as an undocumented accounting discrepancy between the two liquidation code paths, since it materially reduces expected insurance fund inflows relative to what the classic liquidation fee schedule implies.

### Proof of Concept
1. An account becomes unhealthy with e.g. $100 in collateral and $90 in debt.
2. Liquidator A uses classic liquidation (`lending_account_liquidate`): repaying ~$9 of debt via seizing ~$10 of collateral results in `insurance_fund_fee = liab_amount_liquidator - liab_amount_final` being computed and transferred to `bank_insurance_vault` [11](#0-10) [2](#0-1) .
3. Liquidator B instead uses `start_liquidation` → withdraw collateral → repay debt → `end_liquidation` on an equivalent account. `end_receivership` only checks that `seized <= repaid * max_fee` and charges a flat SOL fee to the global fee wallet; no portion of the repaid value is credited to any bank's insurance vault [3](#0-2) .
4. Comparing the two, Liquidator B keeps the full liquidation discount (up to 10%) while the insurance fund receives $0, whereas Liquidator A's liquidation contributed 2.5% of the repaid value to the insurance fund — demonstrating the same fee-bypass pattern described in the external report, where one execution path for an economically equivalent operation systematically avoids a fee that the other path always charges.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate.rs (L237-271)
```rust
        let final_discount: I80F48 =
            I80F48::ONE - (LIQUIDATION_INSURANCE_FEE + LIQUIDATION_LIQUIDATOR_FEE);
        let liquidator_discount: I80F48 = I80F48::ONE - LIQUIDATION_LIQUIDATOR_FEE;

        // Quantity of liability to be paid off by liquidator
        let liab_amount_liquidator: I80F48 = calc_amount(
            calc_value(
                asset_amount,
                asset_price,
                asset_bank.get_balance_decimals(),
                Some(liquidator_discount),
            )?,
            liab_price,
            liab_bank.get_balance_decimals(),
        )?;

        // Quantity of liability to be received by liquidatee
        let liab_amount_final: I80F48 = calc_amount(
            calc_value(
                asset_amount,
                asset_price,
                asset_bank.get_balance_decimals(),
                Some(final_discount),
            )?,
            liab_price,
            liab_bank.get_balance_decimals(),
        )?;

        // Insurance fund fee
        let insurance_fund_fee: I80F48 = liab_amount_liquidator - liab_amount_final;

        assert!(
            insurance_fund_fee >= I80F48::ZERO,
            "Insurance fund fee cannot be negative"
        );
```

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate.rs (L384-401)
```rust
            // ## SPL transfer ##
            // Insurance fund receives fee
            liab_bank.withdraw_spl_transfer(
                insurance_fee_to_transfer,
                ctx.accounts.bank_liquidity_vault.to_account_info(),
                ctx.accounts.bank_insurance_vault.to_account_info(),
                ctx.accounts
                    .bank_liquidity_vault_authority
                    .to_account_info(),
                maybe_liab_bank_mint.as_ref(),
                ctx.accounts.token_program.to_account_info(),
                bank_signer!(
                    BankVaultType::Liquidity,
                    ctx.accounts.liab_bank.key(),
                    liab_bank_liquidity_authority_bump
                ),
                ctx.remaining_accounts,
            )?;
```

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate_end.rs (L43-71)
```rust
    let (seized, seized_f64, repaid, repaid_f64) = end_receivership(
        &mut marginfi_account,
        &mut liq_record,
        ctx.remaining_accounts,
        ignore_healthy,
    )?;

    // Liquidator's allowed fee cannot go lower than the bonus fee minimum
    let fee_state_max_fee: I80F48 = fee_state.liquidation_max_fee.into();
    let max_fee: I80F48 = I80F48::max(
        I80F48!(1) + fee_state_max_fee,
        I80F48!(1) + LIQUIDATION_BONUS_FEE_MINIMUM,
    );

    // Ensure seized asset‐value ≤ N% of repaid liability‐value, where N = 100% + the bonus fee
    if !ignore_healthy {
        check!(
            seized <= repaid * max_fee,
            MarginfiError::LiquidationPremiumTooHigh
        );
    }

    let liquidation_flat_sol_fee = fee_state.liquidation_flat_sol_fee;
    if liquidation_flat_sol_fee > 0 {
        anchor_lang::system_program::transfer(
            ctx.accounts.transfer_flat_fee(),
            liquidation_flat_sol_fee as u64,
        )?;
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate_end.rs (L112-181)
```rust
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

    let seized: I80F48 = pre_assets_equity - post_assets_equity;
    let repaid: I80F48 = pre_liabs_equity - post_liabilities_equity;

    // clear receivership
    marginfi_account.unset_flag(ACCOUNT_IN_RECEIVERSHIP, false);
    liq_record.liquidation_receiver = Pubkey::default();

    let seized_f64 = seized.to_num::<f64>();
    let repaid_f64 = repaid.to_num::<f64>();
    // record the entry in the liquidation record
    {
        // Rotate left to eject the oldest entry
        liq_record.entries.rotate_left(1);
        let entry = &mut liq_record.entries[3];

        entry.asset_amount_seized = seized_f64.to_le_bytes();
        entry.liab_amount_repaid = repaid_f64.to_le_bytes();
        entry.timestamp = Clock::get()?.unix_timestamp;
    }

    Ok((
        seized,
        seized.to_num::<f64>(),
        repaid,
        repaid.to_num::<f64>(),
    ))
}
```

**File:** guides/RISK_AND_LIQUIDATORS/RISK_PARAMETERS.md (L132-152)
```markdown
## Liquidation Parameters

The protocol has two liquidation mechanisms, each with their own fee structure:

### Classic Liquidation

- **Liquidator fee**: 2.5% of the repaid amount (goes to the liquidator as profit)
- **Insurance fee**: 2.5% of the repaid amount (goes to the bank's insurance fund)
- Total discount to the liquidatee: ~5%

The liquidator chooses an asset to seize and a liability to repay. The exchange rate is the oracle
price adjusted by these fees. A liquidation cannot make an account healthy; the liquidator can only
bring maintenance health up to zero.

### Receivership Liquidation

- **Max fee**: Configurable via `FeeState.liquidation_max_fee` (historically ~10%)
- **Flat SOL fee**: A small SOL fee charged per liquidation

The receiver gets temporary control of the account and can withdraw collateral / repay debts. The
protocol enforces that the receiver does not extract more than the max fee as profit.
```

**File:** guides/ADMIN/COLLECTING_FEES.md (L17-22)
```markdown
- **Legacy Liquidation fee** - When using lending_account_liquidation, 2.5% of the amount repaid by
  the liquidator goes to insurance, and 2.5% goes to the liquidator.
- **Liquidation flat sol fee** - A fixed amount of SOL charged when liquidating with receivership liquidation
- **Receivership liquidation fee** - The liquidator collects a profit when performing with
  receivership liquidation. The profit equals the amount withdrawn minus the amount repaid, in
  dollars, in equity terms.
```

**File:** guides/ADMIN/COLLECTING_FEES.md (L47-59)
```markdown
## How Interest Accumulates

When any user interacts with marginfi in a way that changes the balance of a bank,
accrue_interest runs first. Based on the rate and how much time has elapsed, the program adds to the
following bank fields:
```
collected_insurance_fees_outstanding
collected_group_fees_outstanding
collected_program_fees_outstanding
```
This (along with the base rate itself) causes the `asset_share_value` and/or `liability_share_value`
to increase.

```

**File:** guides/RISK_AND_LIQUIDATORS/RECEIVERSHIP_LIQUIDATION.md (L28-43)
```markdown
## Why Use This Instead of Classic Liquidation?

Classic liquidation requires the liquidator themselves to have an account, and maintain collateral
in it. This approach requires no account and no collateral: you can start liquidating with zero
dollars (as long as you have SOL to pay gas fees).

Maximum profits are higher with receivership liquidation, up to 10% as of November 2025, where
classic only nets a 2.5% profit. The maximum profit is defined as:

```
Seized <= Repaid * (1 + max_fee)
```

Where Seized is the equity value withdrawn, in \$, Repaid is the equity value repaid, in \$, and max
fee is the maximum allowed profit currently configured at 10%. Note that equity value is the price
of the token without any weights applied, but inclusive of oracle confidence interval adjustments.
```

**File:** guides/RISK_AND_LIQUIDATORS/CLASSIC_LIQUIDATION.md (L1-3)
```markdown
### Disclaimer

Try Receivership Liquidation instead: the process is simpler, and the potential premium paid is higher. This page describes the "Legacy" or "Classic" liquidation approach, which we will support indefinitely.
```
