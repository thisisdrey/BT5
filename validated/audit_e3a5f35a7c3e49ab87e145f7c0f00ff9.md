## Finding

### Title
Permissionless staked-bank creation lets an unprivileged caller multiply a group's `total_asset_value_init_limit`/`deposit_limit` per validator by creating N banks for the same LST - ([File: programs/marginfi/src/instructions/marginfi_group/add_pool_permissionless.rs])

### Summary
`lending_pool_add_bank_permissionless` is callable by anyone with an arbitrary `bank_seed`, and copies `staked_settings.total_asset_value_init_limit` / `staked_settings.deposit_limit` verbatim into each new bank's config. Because the bank PDA is `[marginfi_group, bank_mint, bank_seed]` and there is no check preventing multiple banks sharing the same `bank_mint`/validator within a group, an attacker can spin up N banks for one validator's LST, each independently capped at the group's limit, giving up to N× the intended collateral-value protection.

### Finding Description
The instruction handler builds `default_config` directly from the shared `StakedSettings` account for the group: [1](#0-0) 
and it is exposed with no admin gate other than the caller-supplied `bank_seed`: [2](#0-1) 
The bank PDA uses `bank_seed` as a distinguishing seed component alongside `marginfi_group` and `bank_mint`, so a caller can create as many banks for the same `bank_mint` (and therefore the same validator, since `bank_mint`/`stake_pool`/`validator_vote_account` are all deterministically tied together via `derive_single_pool_keys_from_vote_and_validate_owner`) as they like, merely incrementing `bank_seed`. `group.add_bank()` only increments a counter with no duplicate-mint check: [3](#0-2) 

Critically, `total_asset_value_init_limit` is enforced **per bank**, using only that bank's own `total_asset_shares`, not aggregated across banks that share the same underlying mint/validator: [4](#0-3) 

This means if the attacker splits deposits across N permissionless banks (each staying under the per-bank limit), no bank ever triggers the init-limit discount, and the sum of "protected" collateral value across N banks is N × `settings.total_asset_value_init_limit`, instead of the single limit the group's risk admin configured expecting it to bound total exposure to one validator/LST's oracle risk. `deposit_limit` is likewise multiplied N times since it's a per-bank cap sourced from the same shared settings.

### Impact Explanation
`total_asset_value_init_limit` exists specifically "to limit the damage of oracle attacks" per the field's own documentation, capping the maximum USD-equivalent collateral value that can back initial-margin borrows for a given asset. By permissionlessly creating multiple banks for the same validator's stake, an attacker multiplies this protection's effective ceiling, allowing far more aggregate borrowing power to be backed by one LST than the group admin intended, undermining the systemic risk control this parameter is meant to enforce. Because the underlying deposits are real tokens (not fabricated), this is not itself an insolvency by forged assets, but it defeats the specific "oracle-attack mitigation cap" invariant the question targets, letting an attacker overstate protected borrowing power across many banks for a single validator.

### Likelihood Explanation
Fully permissionless and repeatable: no admin approval, signer restriction, or duplicate-mint check blocks repeated calls to `lending_pool_add_bank_permissionless` with incrementing `bank_seed` for the same validator/vote account. The only requirements are a valid SPL single-pool stake pool/vote account/mint (which is fixed per validator, not attacker-controlled) and paying rent for the new bank/vault accounts, both trivially satisfiable by any user.

### Recommendation
Enforce that `total_asset_value_init_limit` (and/or `deposit_limit`) for staked-collateral banks is scoped per validator/underlying LST mint rather than purely per bank — e.g., reject permissionless bank creation if an active bank for the same `bank_mint` (or `validator_vote_account`) already exists in the group, or aggregate `total_asset_value_init_limit` enforcement across all banks sharing the same underlying mint/validator when computing the init-margin discount.

### Proof of Concept
Rust integration test plan (bankrun/test-utils style, mirroring `tests/specs/staked/s02_addBank.spec.ts`):
1. Init a group and `StakedSettings` with `total_asset_value_init_limit = L` and generous `deposit_limit`.
2. As an unprivileged user, call `lending_pool_add_bank_permissionless` twice for the same validator's `stake_pool`/`bank_mint`, with `bank_seed = 0` and `bank_seed = 1`, producing `bank_0` and `bank_1`.
3. Deposit LST worth `~L` USD into `bank_0` and another `~L` USD into `bank_1` (same underlying validator stake) from one or more accounts.
4. Query health/`pulse_health` for an account holding balances across both banks; assert combined weighted collateral value ≈ `2L` (no per-bank discount triggered), exceeding the single `L` cap `settings.total_asset_value_init_limit` was set to protect.
5. Compare against a baseline single-bank scenario where depositing `2L` worth into one bank correctly triggers `maybe_get_asset_weight_init_discount` and caps counted value at `L`, demonstrating the N-bank bypass.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/add_pool_permissionless.rs (L72-89)
```rust
    let default_config: BankConfigCompact = BankConfigCompact {
        asset_weight_init: settings.asset_weight_init,
        asset_weight_maint: settings.asset_weight_maint,
        liability_weight_init: I80F48!(1.5).into(), // placeholder
        liability_weight_maint: I80F48!(1.25).into(), // placeholder
        deposit_limit: settings.deposit_limit,
        interest_rate_config: default_ir_config.into(), // placeholder
        operational_state: BankOperationalState::Operational,
        borrow_limit: 0,
        risk_tier: settings.risk_tier,
        asset_tag: ASSET_TAG_STAKED,
        config_flags: PYTH_PUSH_MIGRATED_DEPRECATED,
        _pad0: [0; 5],
        total_asset_value_init_limit: settings.total_asset_value_init_limit,
        oracle_max_age: settings.oracle_max_age,
        // Note: this will use the default of 10%. SOL oracle confidence is generally fine.
        oracle_max_confidence: 0,
    };
```

**File:** programs/marginfi/src/instructions/marginfi_group/add_pool_permissionless.rs (L186-237)
```rust
#[derive(Accounts)]
#[instruction(bank_seed: u64)]
pub struct LendingPoolAddBankPermissionless<'info> {
    #[account(mut)]
    pub marginfi_group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        has_one = marginfi_group @ MarginfiError::InvalidGroup
    )]
    pub staked_settings: AccountLoader<'info, StakedSettings>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    /// Mint of the spl-single-pool LST (a PDA derived from `stake_pool`)
    ///
    /// CHECK: passing a mint here that is not actually a staked collateral LST is not possible
    /// because the sol_pool and stake_pool will not derive to a valid PDA which is also owned by
    /// the staking program and spl-single-pool program.
    pub bank_mint: Box<InterfaceAccount<'info, Mint>>,

    /// CHECK: Validated using `stake_pool`
    pub sol_pool: UncheckedAccount<'info>,

    /// CHECK: Validated using `stake_pool` and native stake-program ownership.
    pub pool_onramp: UncheckedAccount<'info>,

    /// CHECK: We validate this is correct backwards, by deriving the PDA of the `bank_mint` using
    /// this key.
    ///
    /// If derives the same `bank_mint`, then this must be the correct stake pool for that mint, and
    /// we can subsequently use it to validate the `sol_pool`
    pub stake_pool: UncheckedAccount<'info>,

    /// Validator vote account for this staked bank.
    ///
    /// CHECK: validated in handler by enforcing vote-account owner and PDA chain:
    /// vote -> stake_pool -> mint/stake/on-ramp.
    pub validator_vote_account: UncheckedAccount<'info>,

    #[account(
        init,
        space = 8 + std::mem::size_of::<Bank>(),
        payer = fee_payer,
        seeds = [
            marginfi_group.key().as_ref(),
            bank_mint.key().as_ref(),
            &bank_seed.to_le_bytes(),
        ],
        bump,
    )]
    pub bank: AccountLoader<'info, Bank>,
```

**File:** programs/marginfi/src/state/marginfi_group.rs (L182-191)
```rust
    // Increment the bank count by 1. If you managed to create 16,000 banks, congrats, does
    // nothing.
    fn add_bank(&mut self) -> MarginfiResult {
        self.banks = self.banks.saturating_add(1);

        let clock = Clock::get()?;
        self.fee_state_cache.last_update = clock.unix_timestamp;

        Ok(())
    }
```

**File:** programs/marginfi/src/state/bank.rs (L321-360)
```rust
    fn maybe_get_asset_weight_init_discount(
        &self,
        price: I80F48,
    ) -> MarginfiResult<Option<I80F48>> {
        if self.config.usd_init_limit_active() {
            let bank_total_assets_value = calc_value(
                self.get_asset_amount(self.total_asset_shares.into())?,
                price,
                self.get_balance_decimals(),
                None,
            )?;

            let total_asset_value_init_limit =
                I80F48::from_num(self.config.total_asset_value_init_limit);

            #[cfg(target_os = "solana")]
            debug!(
                "Init limit active, limit: {}, total_assets: {}",
                total_asset_value_init_limit, bank_total_assets_value
            );

            if bank_total_assets_value > total_asset_value_init_limit {
                let discount = total_asset_value_init_limit
                    .checked_div(bank_total_assets_value)
                    .ok_or_else(math_error!())?;

                #[cfg(target_os = "solana")]
                debug!(
                    "Discounting assets by {:.2} because of total deposits {} over {} usd cap",
                    discount, bank_total_assets_value, total_asset_value_init_limit
                );

                Ok(Some(discount))
            } else {
                Ok(None)
            }
        } else {
            Ok(None)
        }
    }
```
