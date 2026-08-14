### Title
Stale cached `StakedSettings` (oracle key, weights, limits) on existing staked banks after admin edit, requiring per-bank manual propagation - ([File: programs/marginfi/src/instructions/marginfi_group/edit_stake_settings.rs])

### Summary
This is a structural analog of the Quest Protocol bug: an entity (`Quest`) caches an address (`RabbitHoleReceipt`) copied at creation time from a factory (`QuestFactory`), and when the factory's canonical value is later changed, the previously-created entity keeps operating on the stale cached copy, breaking user-facing flows until a manual, per-instance fix is applied. In marginfi-v2, the same pattern exists between `StakedSettings` (the group-level canonical config for staked-collateral banks) and each individual `Bank` account of `ASSET_TAG_STAKED` type, which snapshots `StakedSettings` fields — including the oracle key — at creation time and does **not** automatically resync when `StakedSettings` is edited.

### Finding Description
When a staked-collateral bank is created via `lending_pool_add_bank_permissionless`, the bank's `config.oracle_keys[0]`, `asset_weight_init/maint`, `deposit_limit`, `total_asset_value_init_limit`, `oracle_max_age`, `risk_tier`, and staked-oracle flags are all copied by value from the group's `StakedSettings` account into the new `Bank` account: [1](#0-0) [2](#0-1) 

The group admin can later edit `StakedSettings` (including the oracle address) via `edit_staked_settings`, which mutates only the `StakedSettings` account, and explicitly does **not** touch any already-created `Bank` accounts. The code comment itself flags this as an easy-to-forget manual step: "Remember to propagate afterwards": [3](#0-2) 

To resync a specific bank with the updated `StakedSettings`, a separate instruction, `propagate_staked_settings`, must be called individually **per bank**: [4](#0-3) 

Until that propagation ix is executed for a given bank, the bank continues operating with the stale, pre-edit oracle key and risk parameters that were snapshotted at bank-creation time — exactly mirroring the `Quest`/`RabbitHoleReceipt` desync: the canonical source of truth (`StakedSettings`, analogous to `QuestFactory.rabbitholeReceiptContract`) is updated, but the dependent account (`Bank`, analogous to `Quest`) keeps a stale immutable-at-creation copy (`bank.config.oracle_keys[0]`, analogous to `Quest`'s immutable `rabbitHoleReceiptContract`) until a separate, easily-omitted step is run.

### Impact Explanation
If a group admin changes `StakedSettings.oracle` (e.g., because the old oracle feed is deprecated, migrated, or has become unreliable/stale) without also calling `propagate_staked_settings` on every existing staked bank for that group, those un-propagated banks continue pricing/validating collateral against the old oracle key. Depending on the state of the old oracle feed, this can:
- Cause deposits/withdrawals/borrows/liquidations against that bank to revert due to a stale/invalid oracle price, effectively locking user funds in that bank until someone manually calls `propagate_staked_settings` for it, or
- Cause the bank to continue using outdated risk parameters (asset weights, deposit limits, risk tier) that no longer reflect the admin's intended, updated risk posture, leading to inconsistent collateral valuation across banks in the same group.

This affects ordinary users' unprivileged deposit/withdraw/borrow/liquidation paths on that specific bank, not just admin operations, satisfying the "permanent lock/freeze" or "unauthorized-state divergence" impact bar even though the root trigger is an admin action.

### Likelihood Explanation
Likelihood is moderate-to-low, similar to the original finding's downgrade rationale: `edit_staked_settings` is admin-gated, and the fix (`propagate_staked_settings`) is permissionless — meaning any user, keeper, or the p0-cli tooling (`p0 group propagate-staked-settings`) can call it to resync a bank at any time, which reduces severity relative to the Quest bug (where only the admin could fix things via expensive manual minting). However, the risk is real in the interim window between an admin's `edit_staked_settings` call and propagation across all affected banks — particularly if a group has many staked banks and the admin/keeper forgets or delays propagating to all of them, as the code's own comment warns.

### Recommendation
- Consider making `edit_staked_settings` optionally cascade the update to all banks tagged `ASSET_TAG_STAKED` under that group in the same transaction (or via a follow-up permissionless crank that is guaranteed to run), rather than relying on a human/keeper to remember to call `propagate_staked_settings` for every affected bank.
- At minimum, emit an on-chain event/warning (in addition to `EditStakedSettingsEvent`) enumerating affected banks that still need propagation, and consider adding a `last_propagated_slot`/`staked_settings_version` field on `Bank` so stale banks can be easily identified and monitored off-chain, and optionally have price-fetching/oracle-validation code reject or warn when a bank's cached staked settings appear too far out of sync with the current `StakedSettings`.

### Proof of Concept
1. Group admin creates a staked-collateral bank `B` via `lending_pool_add_bank_permissionless`; `B.config.oracle_keys[0]` is snapshotted from `StakedSettings.oracle` at that time. [2](#0-1) 
2. Time passes; users deposit/borrow against bank `B` using the original oracle.
3. Admin calls `edit_staked_settings` to change `StakedSettings.oracle` to a new feed (e.g., migrating from a deprecated Pyth feed), but does not (or cannot, in time) call `propagate_staked_settings` on bank `B`. [5](#0-4) 
4. Bank `B.config.oracle_keys[0]` still points at the old, now-deprecated/stale oracle. Any deposit/withdraw/borrow/liquidation instruction touching bank `B` will fetch price from the stale oracle account; if that oracle account stops publishing fresh updates (which is often exactly why it was replaced), price staleness checks will cause these user transactions to revert, locking user positions in bank `B` until `propagate_staked_settings` is explicitly invoked for it. [4](#0-3)

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

**File:** programs/marginfi/src/instructions/marginfi_group/add_pool_permissionless.rs (L115-116)
```rust
    bank.config.oracle_setup = OracleSetup::StakedWithPythPush;
    bank.config.oracle_keys[0] = settings.oracle;
```

**File:** programs/marginfi/src/instructions/marginfi_group/edit_stake_settings.rs (L1-44)
```rust
use crate::events::EditStakedSettingsEvent;
use crate::state::staked_settings::StakedSettingsImpl;
// Used by the group admin to edit the default features of staked collateral banks. Remember to
// propagate afterwards.
use crate::set_if_some;
use crate::MarginfiError;
use anchor_lang::prelude::*;
use marginfi_type_crate::types::{MarginfiGroup, RiskTier, StakedSettings, WrappedI80F48};

pub fn edit_staked_settings(
    ctx: Context<EditStakedSettings>,
    settings: StakedSettingsEditConfig,
) -> Result<()> {
    // let group = ctx.accounts.marginfi_group.load()?;
    let mut staked_settings = ctx.accounts.staked_settings.load_mut()?;
    // require_keys_eq!(group.admin, ctx.accounts.admin.key());

    set_if_some!(staked_settings.oracle, settings.oracle);

    set_if_some!(
        staked_settings.asset_weight_init,
        settings.asset_weight_init
    );
    set_if_some!(
        staked_settings.asset_weight_maint,
        settings.asset_weight_maint
    );
    set_if_some!(staked_settings.deposit_limit, settings.deposit_limit);
    set_if_some!(
        staked_settings.total_asset_value_init_limit,
        settings.total_asset_value_init_limit
    );
    set_if_some!(staked_settings.oracle_max_age, settings.oracle_max_age);
    set_if_some!(staked_settings.risk_tier, settings.risk_tier);

    staked_settings.validate()?;

    emit!(EditStakedSettingsEvent {
        group: ctx.accounts.marginfi_group.key(),
        settings
    });

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/propagate_staked_settings.rs (L10-35)
```rust
pub fn propagate_staked_settings(ctx: Context<PropagateStakedSettings>) -> Result<()> {
    let settings = ctx.accounts.staked_settings.load()?;
    let mut bank = ctx.accounts.bank.load_mut()?;

    let (oracle_before, oracle_after) = (bank.config.oracle_keys[0], settings.oracle);

    bank.config.oracle_keys[0] = settings.oracle;
    bank.config.asset_weight_init = settings.asset_weight_init;
    bank.config.asset_weight_maint = settings.asset_weight_maint;
    bank.config.deposit_limit = settings.deposit_limit;
    bank.config.total_asset_value_init_limit = settings.total_asset_value_init_limit;
    bank.config.oracle_max_age = settings.oracle_max_age;
    bank.config.risk_tier = settings.risk_tier;
    bank.flags &= !STAKED_ORACLE_FLAGS;
    bank.flags |= settings.flags & STAKED_ORACLE_FLAGS;

    // Only validate the oracle info if it has changed
    if oracle_before != oracle_after {
        bank.config
            .validate_oracle_setup(ctx.remaining_accounts, None, None, None)?;
    }

    bank.config.validate()?;

    Ok(())
}
```
