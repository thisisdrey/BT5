Based on my investigation, this is the analog I found.

### Title
Stale `Balance.bank_asset_tag` cached at position-open time is never revalidated after admin re-tags a bank, allowing forbidden asset comingling that breaks isolation invariants used by the risk engine - (File: `type-crate/src/types/mod.rs`, `type-crate/src/types/user_account.rs`, `programs/marginfi/src/state/marginfi_account.rs`)

### Summary
The reported bug class is: a classification/origin field on an untrusted or stale input is not revalidated against the current source of truth, so a downstream component keys off the wrong value, and the final computed state (or fraud outcome) diverges from what an honest execution would produce. The `marginfi-v2` analog is the `Balance.bank_asset_tag` field, which is a **cached copy** of `bank.config.asset_tag` written once when a position is opened, and used afterward (instead of re-reading the live `bank.config.asset_tag`) to enforce which asset classes are allowed to coexist on an account.

### Finding Description
When a new balance is created, `BankAccountWrapper::find_or_create` snapshots the bank's current tag into the balance: [1](#0-0) 

The `Balance` struct explicitly documents this as a permanent, non-refreshing snapshot: "Inherited from the bank when the position is first created and CANNOT BE CHANGED after that." [2](#0-1) 

This cached `bank_asset_tag`, not the bank's live `config.asset_tag`, is what the comingling-safety check (`validate_asset_tags`) iterates over to decide whether a new deposit/borrow is allowed to coexist with existing balances (e.g., preventing `ASSET_TAG_STAKED` positions from mixing with `ASSET_TAG_DEFAULT`-like positions): [3](#0-2) 

This check is invoked on every deposit and borrow: [4](#0-3) [5](#0-4) 

However, an admin can later reconfigure a bank's `asset_tag` via `configure()` on `Bank`, with no code path that walks existing accounts and updates their already-opened balances' `bank_asset_tag`: [6](#0-5) 

Because the isolation check trusts the stale, account-side snapshot rather than re-deriving it from the live bank config, if a bank's `asset_tag` is changed after users have already opened balances against it (a scenario explicitly exercised for other purposes in `tests/specs/drift/d06_driftBankInit.spec.ts:240-259`, which retags a bank post-creation), the account's existing `bank_asset_tag` no longer matches the bank's real classification. A user could exploit the resulting inconsistency window: open a position while a bank carries one tag (e.g., `ASSET_TAG_DEFAULT`), have the tag retagged (e.g., to `ASSET_TAG_STAKED`) by governance for a legitimate reason, and then deposit into a genuinely-staked bank — the stale cached tag on the earlier balance would still read `ASSET_TAG_DEFAULT`, bypassing the "Staked can only comingle with Staked/SOL" restriction described in the config docs. This is functionally the same root cause as the reported analog: a cached classification field (queue origin vs. live enqueue source; asset tag on balance vs. live bank config) is trusted for a security-relevant branch instead of being revalidated, letting stale/attacker-influenced state produce an outcome (forbidden comingling, and by extension incorrect health/risk-tier accounting) that a fully-consistent evaluation would have rejected.

### Impact Explanation
If exploitable, this would let an account hold combinations of asset tags that the protocol's asset-tag policy is specifically designed to forbid (e.g., mixing isolated/staked collateral with default-tier collateral). Because risk weights, price confidence bands, and liquidation/health logic are designed around these tag partitions, comingling could let a user inflate borrowing power against collateral that was never meant to back that liability class, potentially leading to under-collateralized positions and protocol bad debt upon liquidation.

### Likelihood Explanation
This requires an admin/governance action (re-tagging a bank's `asset_tag` after it already has open user positions) as a precondition, which is a privileged action, not something an unprivileged attacker fully controls end-to-end. I could not confirm from the available code/tests whether `configure()` is actually invoked on banks with pre-existing balances in production operational practice, nor whether there is a separate guard elsewhere (e.g., in the risk engine's health calculation, which I did not find using `bank_asset_tag` for isolation, only `validate_asset_tags`/`validate_bank_asset_tags`) that would catch this at withdrawal/liquidation time. This uncertainty significantly limits confidence that this is a practically-triggerable, purely-unprivileged vulnerability; it is best characterized as a stale-cache/consistency gap contingent on an admin action, rather than a confirmed unilateral exploit.

### Recommendation
Re-derive the comingling check from the bank's live `config.asset_tag` at the time of validation instead of (or in addition to) the account-stored `bank_asset_tag`, or add a reconciliation step in `configure()` (or a permissionless backfill instruction) that walks/flags existing balances referencing a bank whose `asset_tag` changed, forcing re-validation (e.g., blocking further deposits/borrows on the affected balances) until the stale tag is corrected.

### Proof of Concept
Not independently reproduced against a live program; the trace above is based on static code reading of `find_or_create`, `Balance::bank_asset_tag`, `validate_asset_tags`, and `Bank::configure`, plus the observed test pattern in `tests/specs/drift/d06_driftBankInit.spec.ts` showing that bank `asset_tag` retagging after bank creation is a supported admin operation. I was not able to confirm within available context whether existing balances' `bank_asset_tag` is reconciled elsewhere (e.g., during health/risk engine evaluation) — this should be verified in a live/test environment (e.g., a Devin session with full repo and test-execution access) before treating this as a confirmed, exploitable finding.

### Citations

**File:** programs/marginfi/src/state/marginfi_account.rs (L1556-1567)
```rust
                lending_account.balances[empty_index] = Balance {
                    active: 1,
                    bank_pk: *bank_pk,
                    bank_asset_tag: bank.config.asset_tag,
                    tag: 0,
                    _pad0: [0; 4],
                    asset_shares: I80F48::ZERO.into(),
                    liability_shares: I80F48::ZERO.into(),
                    emissions_outstanding: I80F48::ZERO.into(),
                    last_update: Clock::get()?.unix_timestamp as u64,
                    _padding: [0; 1],
                };
```

**File:** type-crate/src/types/user_account.rs (L286-289)
```rust
    pub bank_pk: Pubkey,
    /// Inherited from the bank when the position is first created and CANNOT BE CHANGED after that.
    /// Note that all balances created before the addition of this feature use `ASSET_TAG_DEFAULT`
    pub bank_asset_tag: u8,
```

**File:** type-crate/src/types/mod.rs (L74-116)
```rust
/// balances, or all Staked/Sol balances. Default and Staked assets cannot mix.
pub fn validate_asset_tags(bank: &Bank, marginfi_account: &MarginfiAccount) -> bool {
    let mut has_default_asset = false;
    let mut has_staked_asset = false;

    let is_default_like = |asset_tag: u8| {
        matches!(
            asset_tag,
            ASSET_TAG_DEFAULT
                | ASSET_TAG_KAMINO
                | ASSET_TAG_DRIFT
                | ASSET_TAG_SOLEND
                | ASSET_TAG_JUPLEND
        )
    };

    for balance in marginfi_account.lending_account.balances.iter() {
        if balance.is_active() {
            match balance.bank_asset_tag {
                ASSET_TAG_DEFAULT => has_default_asset = true,
                ASSET_TAG_SOL => { /* Do nothing, SOL can mix with any asset type */ }
                ASSET_TAG_STAKED => has_staked_asset = true,
                // Kamino/Drift/Solend/JupLend assets behave like default assets
                ASSET_TAG_KAMINO | ASSET_TAG_DRIFT | ASSET_TAG_SOLEND | ASSET_TAG_JUPLEND => {
                    has_default_asset = true
                }
                _ => panic!("unsupported asset tag"),
            }
        }
    }

    // 1. Default-like assets cannot mix with Staked assets
    if is_default_like(bank.config.asset_tag) && has_staked_asset {
        return false;
    }

    // 2. Staked SOL cannot mix with Default-like assets
    if bank.config.asset_tag == ASSET_TAG_STAKED && has_default_asset {
        return false;
    }

    true
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L56-60)
```rust
    let mut bank = bank_loader.load_mut()?;
    let mut marginfi_account = marginfi_account_loader.load_mut()?;
    let group = marginfi_group_loader.load()?;
    validate_asset_tags(&bank, &marginfi_account)?;
    validate_bank_state(&bank, InstructionKind::FailsIfPausedOrReduceState)?;
```

**File:** programs/marginfi/src/instructions/marginfi_account/borrow.rs (L88-91)
```rust
        let mut bank = bank_loader.load_mut()?;

        validate_asset_tags(&bank, &marginfi_account)?;
        validate_bank_state(&bank, InstructionKind::FailsIfPausedOrReduceState)?;
```

**File:** programs/marginfi/src/state/bank.rs (L456-457)
```rust
        set_if_some!(self.config.asset_tag, config.asset_tag);

```
