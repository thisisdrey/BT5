Let me analyze the external bug's root cause: a stale snapshot of a parameter (captured at proposal creation) is forwarded to a check that uses a live constraint (which can be raised independently), causing permanent failure. I'll search for analogs in the Aptos repository.

### Title
`update_governance_config` can raise `voting_duration_secs` above `recurring_lockup_duration_secs`, permanently bricking Aptos on-chain governance — (`aptos-move/framework/aptos-framework/sources/aptos_governance.move`)

---

### Summary

`aptos_governance::create_proposal_v2_impl` enforces that the proposer's stake lockup exceeds the proposal's expiration time, computed as `now + voting_duration_secs`. A validator's maximum achievable lockup is `now + recurring_lockup_duration_secs`. The two parameters are independently mutable via separate governance proposals with no cross-validation. If a governance proposal raises `voting_duration_secs` to a value ≥ `recurring_lockup_duration_secs`, no validator can ever satisfy the lockup check, permanently bricking all future proposal creation and voting. Recovery requires an out-of-band emergency intervention because every on-chain fix path also requires governance.

---

### Finding Description

**Proposal creation lockup check:**

In `create_proposal_v2_impl`, the proposer's lockup is validated against the proposal's expiration:

```
let proposal_expiration = current_time + governance_config.voting_duration_secs;
assert!(
    stake_pool_is_eligible_to_vote(stake_pool, proposal_expiration),
    error::invalid_argument(EINSUFFICIENT_STAKE_LOCKUP),
);
```

where `stake_pool_is_eligible_to_vote` is:

```
inline fun stake_pool_is_eligible_to_vote(
    stake_pool: address, proposal_expiration: u64
): bool {
    proposal_expiration < stake::get_lockup_secs(stake_pool)
}
``` [1](#0-0) [2](#0-1) 

The same check is applied at vote time: [3](#0-2) 

**Maximum achievable lockup is bounded by `recurring_lockup_duration_secs`:**

At epoch transition, every active validator's expired lockup is renewed to exactly `now + recurring_lockup_duration_secs`:

```
if (stake_pool.locked_until_secs <= reconfig_start_secs) {
    stake_pool.locked_until_secs = now_secs + recurring_lockup_duration_secs;
}
``` [4](#0-3) 

`increase_lockup_with_cap` also sets `locked_until_secs = now + recurring_lockup_duration_secs`: [5](#0-4) 

So the maximum `locked_until_secs` any validator can hold is `now + recurring_lockup_duration_secs`.

**No cross-validation between the two parameters:**

`update_governance_config` accepts any `voting_duration_secs` without checking it against `recurring_lockup_duration_secs`:

```
public fun update_governance_config(
    aptos_framework: &signer,
    min_voting_threshold: u128,
    required_proposer_stake: u64,
    voting_duration_secs: u64,
) acquires GovernanceConfig {
    system_addresses::assert_aptos_framework(aptos_framework);
    let governance_config = borrow_global_mut<GovernanceConfig>(@aptos_framework);
    governance_config.voting_duration_secs = voting_duration_secs;
    ...
}
``` [6](#0-5) 

`update_recurring_lockup_duration_secs` accepts any positive value without checking it against `voting_duration_secs`: [7](#0-6) 

**The invariant that must hold but is never enforced:**

For any validator to create a proposal or vote, the following must be true:

```
now + voting_duration_secs < locked_until_secs ≤ now + recurring_lockup_duration_secs
```

This requires `voting_duration_secs < recurring_lockup_duration_secs`. Neither setter enforces this.

**The brick is permanent:**

Once `voting_duration_secs ≥ recurring_lockup_duration_secs`:

1. No validator can create a proposal (`EINSUFFICIENT_STAKE_LOCKUP` abort).
2. No validator can vote on any proposal (same lockup check at vote time).
3. `update_governance_config` requires `@aptos_framework` signer, obtainable only via `resolve`/`resolve_multi_step_proposal`.
4. `resolve` requires a proposal to have passed, which requires voting, which is bricked.
5. `update_recurring_lockup_duration_secs` also requires `@aptos_framework` signer — same dead end. [8](#0-7) 

---

### Impact Explanation

Permanent freeze of Aptos on-chain governance. No new proposals can be created or voted on. All framework upgrades, staking parameter changes, feature flag toggles, and any other governance-gated actions are permanently blocked on-chain. Recovery requires an emergency out-of-band validator-level intervention (hard fork or emergency write-set transaction), which is a severe disruption to the network.

---

### Likelihood Explanation

The trigger is a governance proposal calling `update_governance_config` with `voting_duration_secs ≥ recurring_lockup_duration_secs`. This is a plausible, legitimate-looking governance action — longer voting periods are a common governance improvement proposal (the repository even includes an example script `governance_update_voting_duration.move` that does exactly this). There is no code comment, guard, or documentation warning about the hidden invariant linking these two separately-mutable parameters. An honest governance participant proposing to extend the voting period from 7 days to 31 days (while `recurring_lockup_duration_secs` is 30 days) would trigger the brick in good faith. [9](#0-8) 

---

### Recommendation

Add a cross-validation guard in both setters:

**In `update_governance_config`:**
```move
let staking_config = staking_config::get();
let lockup_duration = staking_config::get_recurring_lockup_duration(&staking_config);
assert!(
    voting_duration_secs < lockup_duration,
    error::invalid_argument(EINVALID_VOTING_DURATION)
);
```

**In `update_recurring_lockup_duration_secs`:**
```move
let governance_config = borrow_global<GovernanceConfig>(@aptos_framework);
assert!(
    new_recurring_lockup_duration_secs > governance_config.voting_duration_secs,
    error::invalid_argument(EINVALID_LOCKUP_DURATION)
);
```

Alternatively, document the invariant prominently in both functions and add a view function that exposes the safety margin (`recurring_lockup_duration_secs - voting_duration_secs`) so governance tooling can surface it before a proposal is submitted.

---

### Proof of Concept

```move
// State before: voting_duration_secs = 7 days, recurring_lockup_duration_secs = 30 days
// Governance passes a proposal calling:
aptos_governance::update_governance_config(
    &framework_signer,
    min_voting_threshold,
    required_proposer_stake,
    31 * 24 * 60 * 60, // 31 days — exceeds recurring_lockup_duration_secs (30 days)
);

// After execution:
// proposal_expiration = now + 31 days
// max locked_until_secs = now + 30 days  (set by increase_lockup or epoch renewal)
// Check: proposal_expiration < locked_until_secs
//        now + 31 days < now + 30 days  => FALSE => EINSUFFICIENT_STAKE_LOCKUP abort

// Every subsequent create_proposal call aborts.
// Every vote call aborts (same check at vote time).
// update_governance_config requires @aptos_framework signer => requires resolved proposal => requires votes => bricked.
// update_recurring_lockup_duration_secs requires @aptos_framework signer => same dead end.
// Governance is permanently bricked on-chain.
```

The root cause — two independently-mutable parameters with a hidden ordering invariant and no cross-validation — is identical to the `ArmadaGovernor::queue` / `timelock.updateDelay` pattern in the external report.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L238-258)
```text
    public fun update_governance_config(
        aptos_framework: &signer,
        min_voting_threshold: u128,
        required_proposer_stake: u64,
        voting_duration_secs: u64,
    ) acquires GovernanceConfig {
        system_addresses::assert_aptos_framework(aptos_framework);

        let governance_config = borrow_global_mut<GovernanceConfig>(@aptos_framework);
        governance_config.voting_duration_secs = voting_duration_secs;
        governance_config.min_voting_threshold = min_voting_threshold;
        governance_config.required_proposer_stake = required_proposer_stake;

        event::emit(
            UpdateConfig {
                min_voting_threshold,
                required_proposer_stake,
                voting_duration_secs
            },
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L350-358)
```text
    inline fun stake_pool_is_eligible_to_vote(
        stake_pool: address, proposal_expiration: u64
    ): bool {
        // The voter's stake needs to be locked up at least as long as the proposal's expiration.
        // Also no one can vote on a expired proposal.
        // Note the boundary condition must be strictly less than to avoid the edge case where the
        // proposal expiration is equal to the lockup until.
        proposal_expiration < stake::get_lockup_secs(stake_pool)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L425-431)
```text
        // The proposer's stake needs to be locked up at least as long as the proposal's voting period.
        let current_time = timestamp::now_seconds();
        let proposal_expiration = current_time + governance_config.voting_duration_secs;
        assert!(
            stake_pool_is_eligible_to_vote(stake_pool, proposal_expiration),
            error::invalid_argument(EINSUFFICIENT_STAKE_LOCKUP),
        );
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.move (L600-609)
```text
    /// Resolve a successful single-step proposal. This would fail if the proposal is not successful (not enough votes or more no
    /// than yes).
    public fun resolve(
        proposal_id: u64,
        signer_address: address
    ): signer acquires ApprovedExecutionHashes, GovernanceResponsbility {
        voting::resolve<GovernanceProposal>(@aptos_framework, proposal_id);
        remove_approved_hash(proposal_id);
        get_signer(signer_address)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_governance.spec.move (L393-395)
```text
        let proposal_expiration = proposal.expiration_secs;
        let locked_until_secs = global<stake::StakePool>(stake_pool).locked_until_secs;
        aborts_if proposal_expiration >= locked_until_secs;
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1040-1054)
```text
    public fun increase_lockup_with_cap(owner_cap: &OwnerCapability) acquires StakePool {
        let pool_address = owner_cap.pool_address;
        assert_stake_pool_exists(pool_address);
        let config = staking_config::get();

        let stake_pool = borrow_global_mut<StakePool>(pool_address);
        let old_locked_until_secs = stake_pool.locked_until_secs;
        let new_locked_until_secs =
            timestamp::now_seconds()
                + staking_config::get_recurring_lockup_duration(&config);
        assert!(
            old_locked_until_secs < new_locked_until_secs,
            error::invalid_argument(EINVALID_LOCKUP)
        );
        stake_pool.locked_until_secs = new_locked_until_secs;
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1456-1462)
```text
            if (stake_pool.locked_until_secs <= reconfig_start_secs) {
                spec {
                    assume now_secs + recurring_lockup_duration_secs <= MAX_U64;
                };
                stake_pool.locked_until_secs = now_secs
                    + recurring_lockup_duration_secs;
            };
```

**File:** aptos-move/framework/aptos-framework/sources/configs/staking_config.move (L284-293)
```text
    public fun update_recurring_lockup_duration_secs(
        aptos_framework: &signer,
        new_recurring_lockup_duration_secs: u64,
    ) acquires StakingConfig {
        assert!(new_recurring_lockup_duration_secs > 0, error::invalid_argument(EZERO_LOCKUP_DURATION));
        system_addresses::assert_aptos_framework(aptos_framework);

        let staking_config = borrow_global_mut<StakingConfig>(@aptos_framework);
        staking_config.recurring_lockup_duration_secs = new_recurring_lockup_duration_secs;
    }
```

**File:** aptos-move/move-examples/governance/sources/governance_update_voting_duration.move (L1-17)
```text
script {
    use aptos_framework::aptos_governance;

    fun main(proposal_id: u64) {
        let framework_signer = aptos_governance::resolve(proposal_id, @aptos_framework);
        // Update voting duration of Aptos governance proposals to 1 day. Other params don't change.
        let updated_voting_duration_secs = 24 * 60 * 60;
        let unchanged_min_voting_threshold = aptos_governance::get_min_voting_threshold();
        let unchanged_required_proposer_stake = aptos_governance::get_required_proposer_stake();
        aptos_governance::update_governance_config(
            &framework_signer,
            unchanged_min_voting_threshold,
            unchanged_required_proposer_stake,
            updated_voting_duration_secs,
        );
    }
}
```
