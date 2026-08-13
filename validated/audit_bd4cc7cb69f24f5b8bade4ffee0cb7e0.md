## Title
Direct SOL donations to a Staked-Collateral bank's underlying stake pool / on-ramp account bypass bank pause/reduce-only restrictions on adding new collateral value - (File: `programs/marginfi/src/state/price.rs`)

### Summary
Marginfi's native Staked Collateral banks price their LST-equivalent shares using a live, on-demand read of the SPL single-validator stake pool's lamport balance (plus its "on-ramp" account), rather than relying purely on internally tracked deposit accounting. Because these underlying accounts are owned by the SPL Single Pool program (not marginfi), any unprivileged user can permissionlessly transfer SOL directly into them, inflating the computed NAV/price multiplier for every existing holder of that bank's staked-collateral shares — completely outside of, and unconstrained by, the bank's `operational_state` (`Paused`/`ReduceOnly`). This mirrors the InitCapital finding: relying on a live-queried external balance for collateral valuation, rather than solely on internally-tracked deposit accounting, lets a user add value to a position (and thus its borrowing power) through a path the admin's pause mechanism does not gate.

### Finding Description
Staked-bank pricing computes NAV directly from the live lamport balances of the stake account and on-ramp account: [1](#0-0) 

This NAV feeds a `price_multiplier` applied to the oracle price for that validator's LST, used for every account holding shares of that bank: [2](#0-1) 

The tests explicitly confirm that anyone can permissionlessly inflate this NAV via a plain `SystemProgram.transfer` to the on-ramp pool account or the stake pool account, with no interaction with marginfi's deposit instruction or its `operational_state` gate at all: [3](#0-2) [4](#0-3) 

By contrast, deposits into a staked bank (like all other bank types) are explicitly gated by `validate_bank_state`, which blocks new deposits when the bank is `Paused` or in `ReduceOnly`: [5](#0-4) 

Documentation confirms the admin's intent behind these states — `Paused` halts all new value being added, and `ReduceOnly`/`ReduceOnlyWithBorrowingPower` are meant to stop new risk from being introduced while existing collateral is (in the `ReduceOnlyWithBorrowingPower` case) still usable for new borrows at its current valuation: [6](#0-5) 

The health/value calculation itself reads `bank.get_asset_amount(balance.asset_shares.into())` times the (possibly donation-inflated) cached price, with `ReduceOnlyWithBorrowingPower` explicitly still counting toward `Initial` margin for new borrows: [7](#0-6) [8](#0-7) 

Because the stake-pool/on-ramp account balances are read live and are not internal marginfi state, an admin's decision to `Pause` or `ReduceOnly` a staked bank (e.g. in response to a suspected validator/oracle issue, or simply to stop new value from entering the bank while winding it down) does not prevent an unprivileged user from continuing to inflate the valuation of every existing depositor's shares in that bank — including their own — via a raw system transfer that never touches the marginfi program or its `operational_state` checks.

### Impact Explanation
This directly parallels the InitCapital [M-05] bug class: an admin-level "stop adding value" control (bank `Paused`/`ReduceOnly`) can be circumvented via a path that bypasses the program's internal accounting gate. Concretely:
- In `ReduceOnlyWithBorrowingPower`, existing collateral still counts toward Initial margin for new borrows; inflating the bank's price multiplier via direct donation increases a user's effective collateral value and borrowing power without ever calling `lending_account_deposit`/staked deposit and without the admin's pause on new value-adding having any effect.
- In `ReduceOnly`, Maintenance-margin value is retained at "full value"; donation-inflated pricing can artificially prop up the Maintenance-margin health of already-unhealthy accounts, delaying or preventing legitimate liquidation while the admin has intentionally frozen the bank to limit further risk.
- The effect is bank-wide rather than tied to a single position (unlike InitCapital's per-`tokenId` bug), but the root cause is identical: valuation derived from a live-queried external balance that unprivileged users can freely manipulate, undermining an admin control meant to gate new collateral value.

### Likelihood Explanation
The donation path requires only a permissionless `SystemProgram.transfer` of SOL to a publicly known PDA (the stake pool or on-ramp account derived from the validator's vote account) — no special privileges, no marginfi instruction, and no bypass of signature checks are needed. It is demonstrated as ordinary, expected behavior in the test suite itself (`s02_addBank.spec.ts`, `s05_solAppreciates.spec.ts`), confirming the mechanism is trivially reachable by any wallet holding SOL.

### Recommendation
Ensure that when a staked-collateral bank is `Paused` or `ReduceOnly`/`ReduceOnlyWithBorrowingPower`, any value increase attributable to external donations to the underlying stake pool/on-ramp accounts cannot be used to increase Initial-margin borrowing power or artificially prop up Maintenance-margin health beyond what was already backed at the time the state was set — e.g., by snapshotting/capping the price multiplier at the moment of pause, or by re-deriving collateral value only from internally tracked deposit amounts rather than a live-queried pool balance while the bank is in a restricted operational state.

### Proof of Concept
1. Admin creates or has an existing `Operational` staked-collateral bank for validator V; users hold shares (`asset_shares`) representing deposited stake.
2. Admin sets the bank to `ReduceOnlyWithBorrowingPower` (or `Paused`) to stop new deposits/risk, per `validate_bank_state`: [9](#0-8) 
3. An unprivileged user sends a plain `SystemProgram.transfer` of SOL directly to the validator's SPL single-pool on-ramp (or stake) account — no marginfi instruction involved: [3](#0-2) 
4. On the next price read, `staked_pool_net_asset_value` incorporates the donated lamports into NAV, raising `price_multiplier` for every existing holder's shares: [1](#0-0) 
5. Any account holding staked-collateral shares now shows increased `assetValue`/`assetValueMaint` in its health cache, usable for new borrows (in `ReduceOnlyWithBorrowingPower`) or for evading liquidation (in `ReduceOnly`), entirely bypassing the admin's `Paused`/`ReduceOnly` restriction on adding new collateral value.

### Citations

**File:** programs/marginfi/src/state/price.rs (L106-122)
```rust
fn staked_pool_net_asset_value(
    pool_stake_info: &AccountInfo,
    pool_onramp_info: &AccountInfo,
    rent: &Rent,
) -> MarginfiResult<u64> {
    let pool_rent_exempt_reserve = rent.minimum_balance(pool_stake_info.data_len());
    let onramp_rent_exempt_reserve = rent.minimum_balance(pool_onramp_info.data_len());

    let main_stake_value = pool_stake_info
        .lamports()
        .saturating_sub(pool_rent_exempt_reserve);
    let onramp_value = pool_onramp_info
        .lamports()
        .saturating_sub(onramp_rent_exempt_reserve);

    Ok(main_stake_value.saturating_add(onramp_value))
}
```

**File:** programs/marginfi/src/state/price.rs (L385-395)
```rust
                // Note: exchange rate is `pool_nav / lst_supply`, but we will do the
                // division last to avoid precision loss. Division does not need to be
                // decimal-adjusted because both SOL and stake positions use 9 decimals

                let account_info = &ais[0];
                check_primary_oracle_key(bank_config, account_info)?;

                let mut feed = PythPushOraclePriceFeed::load_checked(account_info, clock, max_age)?;
                let multiplier = I80F48::from_num(sol_pool_adjusted_balance)
                    .checked_div(I80F48::from_num(lst_supply))
                    .ok_or_else(math_error!())?;
```

**File:** tests/specs/staked/s02_addBank.spec.ts (L978-995)
```typescript
  it("(user 0) Adds 9 SOL to the validator 0's on-ramp pool - multiplier changes again", async () => {
    let tx = new Transaction();
    tx.add(
      SystemProgram.transfer({
        fromPubkey: users[0].wallet.publicKey,
        toPubkey: validators[0].splOnRampPool,
        lamports: 9 * LAMPORTS_PER_SOL, // Total canonical NAV now becomes 50
      }),
    );
    tx.recentBlockhash = await getBankrunBlockhash(bankrunContext);
    tx.sign(users[0].wallet);
    await banksClient.processTransaction(tx);

    const priceMultiplierWithOnRamp = await fetchLstPriceMultiplier();

    // (41 + 9) / 40 = 1.25
    assert.approximately(priceMultiplierWithOnRamp, 1.25, 0.000001);
  });
```

**File:** tests/specs/staked/s05_solAppreciates.spec.ts (L94-113)
```typescript
  // Note: there is also some natural appreciation here because a few epochs have elapsed...
  it(
    "v0 stake sol pool grows by " +
      stakeSolAppreciation +
      " SOL (e.g. MEV rewards) - LST price grows",
    async () => {
      let tx = new Transaction();
      tx.add(
        SystemProgram.transfer({
          fromPubkey: wallet.publicKey,
          toPubkey: validators[0].splSolPool,
          lamports: stakeSolAppreciation * LAMPORTS_PER_SOL,
        }),
      );
      tx.recentBlockhash = await getBankrunBlockhash(bankrunContext);
      tx.sign(wallet.payer);
      await banksClient.processTransaction(tx);

      const priceMultiplierAfterAppreciation = await fetchLstPriceMultiplier();
      assert.approximately(priceMultiplierAfterAppreciation, 2.0, 0.000001); // (50 + 30) / 40 = 2
```

**File:** programs/marginfi/src/utils/general.rs (L266-309)
```rust
pub fn validate_bank_state(bank: &Bank, kind: InstructionKind) -> MarginfiResult {
    if bank.config.operational_state == BankOperationalState::KilledByBankruptcy {
        return err!(MarginfiError::BankKilledByBankruptcy);
    }
    // Bank exists but has not completed one-time setup (e.g. JupLend seed deposit). Block every
    // operation until init runs.
    if bank.config.operational_state == BankOperationalState::Uninitialized {
        return err!(MarginfiError::BankUninitialized);
    }

    match kind {
        InstructionKind::FailsInReduceState if bank.config.operational_state.is_reduce_only() => {
            return err!(MarginfiError::BankReduceOnly);
        }

        InstructionKind::FailsInPausedState
            if bank.config.operational_state == BankOperationalState::Paused =>
        {
            return err!(MarginfiError::BankPaused);
        }

        InstructionKind::FailsIfPausedOrReduceState
            if matches!(
                bank.config.operational_state,
                BankOperationalState::Paused
                    | BankOperationalState::ReduceOnly
                    | BankOperationalState::ReduceOnlyWithBorrowingPower
            ) =>
        {
            return match bank.config.operational_state {
                BankOperationalState::Paused => {
                    err!(MarginfiError::BankPaused)
                }
                state if state.is_reduce_only() => {
                    err!(MarginfiError::BankReduceOnly)
                }
                _ => unreachable!(),
            };
        }
        _ => {}
    }

    Ok(())
}
```

**File:** guides/ADMIN/BANK_STATE.md (L27-43)
```markdown
### Operational

Normal operations. All user actions are allowed: deposit, borrow, withdraw, repay, and liquidation.

### ReduceOnly

Only withdrawals and repayments are allowed. New deposits and borrows are blocked. This state is
intended for winding down a bank.

Important nuances for health calculations in ReduceOnly:
- **Initial margin**: assets in a ReduceOnly bank are valued at **$0**. This means users cannot
  open new borrows using ReduceOnly collateral.
- **Maintenance margin**: assets in a ReduceOnly bank retain their **full value**. This means
  existing positions are not immediately liquidatable just because a bank entered ReduceOnly.

This asymmetry is by design: the system prevents new risk from being taken on ReduceOnly assets,
while not force-liquidating users who already hold them.
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L356-393)
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
```

**File:** programs/marginfi/tests/misc/operational_state.rs (L262-339)
```rust
#[tokio::test]
async fn marginfi_group_bank_reduce_only_with_borrowing_power_counts_for_new_loans(
) -> anyhow::Result<()> {
    let test_f = TestFixture::new(Some(TestSettings {
        banks: vec![
            TestBankSetting {
                mint: BankMint::Usdc,
                config: Some(BankConfig {
                    asset_weight_init: I80F48!(0.9).into(),
                    asset_weight_maint: I80F48!(0.95).into(),
                    ..*DEFAULT_USDC_TEST_BANK_CONFIG
                }),
            },
            TestBankSetting {
                mint: BankMint::Sol,
                config: Some(BankConfig {
                    asset_weight_init: I80F48!(0.8).into(),
                    asset_weight_maint: I80F48!(0.9).into(),
                    liability_weight_init: I80F48!(1.1).into(),
                    liability_weight_maint: I80F48!(1.05).into(),
                    ..*DEFAULT_SOL_TEST_BANK_CONFIG
                }),
            },
        ],
        protocol_fees: false,
    }))
    .await;

    let usdc_bank_f = test_f.get_bank(&BankMint::Usdc);
    let sol_bank_f = test_f.get_bank(&BankMint::Sol);

    let lender_mfi_account = test_f.create_marginfi_account().await;
    let lender_token_account_sol = test_f.sol_mint.create_token_account_and_mint_to(100).await;
    lender_mfi_account
        .try_bank_deposit(lender_token_account_sol.key, sol_bank_f, 100, None)
        .await?;

    let borrower_mfi_account = test_f.create_marginfi_account().await;
    let borrower_token_account_usdc = test_f
        .usdc_mint
        .create_token_account_and_mint_to(100_000)
        .await;
    borrower_mfi_account
        .try_bank_deposit(borrower_token_account_usdc.key, usdc_bank_f, 100_000, None)
        .await?;

    usdc_bank_f
        .update_config(
            BankConfigOpt {
                operational_state: Some(BankOperationalState::ReduceOnlyWithBorrowingPower),
                ..Default::default()
            },
            None,
        )
        .await?;

    let extra_lender_mfi_account = test_f.create_marginfi_account().await;
    let extra_lender_token_account_usdc =
        test_f.usdc_mint.create_token_account_and_mint_to(1).await;
    let res = extra_lender_mfi_account
        .try_bank_deposit(extra_lender_token_account_usdc.key, usdc_bank_f, 1, None)
        .await;

    assert!(res.is_err());
    assert_custom_error!(res.unwrap_err(), MarginfiError::BankReduceOnly);

    let borrower_token_account_sol = test_f.sol_mint.create_empty_token_account().await;
    let res = borrower_mfi_account
        .try_bank_borrow(borrower_token_account_sol.key, sol_bank_f, 1)
        .await;

    assert!(
        res.is_ok(),
        "ReduceOnlyWithBorrowingPower collateral should support new borrows"
    );

    Ok(())
}
```
