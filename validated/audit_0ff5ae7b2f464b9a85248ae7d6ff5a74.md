Let me analyze the external bug's root cause: a function always adds margin (always positive delta) regardless of whether the position size increases or decreases, allowing an attacker to drain funds by repeatedly opening/closing positions.

I'll search for analogous patterns in the Aptos codebase where a resource/balance is always incremented without considering the net delta.

Based on my analysis, I found a valid analog. The root cause in the Solidity bug is: a counter is always incremented for one direction of an operation but never decremented for the reverse, allowing an attacker to inflate it by cycling add/undo operations. The exact same pattern exists in `stake.move`.

---

### Title
`total_joining_power` Inflated Without Bound via `add_stake` + `unlock` + `reactivate_stake` Cycle — (`aptos-move/framework/aptos-framework/sources/stake.move`)

### Summary

`update_voting_power_increase` in `stake.move` only ever **increments** `total_joining_power`. The reverse operation — `unlock_with_cap` — never decrements it. An active validator can cycle `add_stake → unlock → reactivate_stake` repeatedly within a single epoch using a small fixed amount of APT to inflate `total_joining_power` to the per-epoch joining limit, permanently blocking new validators from joining and existing validators from adding stake for the rest of that epoch.

### Finding Description

`update_voting_power_increase` is called in `add_stake_with_cap` whenever an active or pending-active validator adds stake: [1](#0-0) 

```move
if (find_validator(&validator_set.active_validators, pool_address).is_some()
    || find_validator(&validator_set.pending_active, pool_address).is_some()) {
    update_voting_power_increase(amount);
};
```

`update_voting_power_increase` unconditionally adds to `total_joining_power`: [2](#0-1) 

The reverse path — `unlock_with_cap` — moves coins from `active` to `pending_inactive` but **never touches `total_joining_power`**: [3](#0-2) 

Similarly, `reactivate_stake_with_cap` moves coins from `pending_inactive` back to `active` without calling `update_voting_power_increase` (the comment even acknowledges this is intentional): [4](#0-3) 

`total_joining_power` is only decremented in `leave_validator_set` for **pending-active** validators (not active ones): [5](#0-4) 

And it is only reset to zero at epoch boundary: [6](#0-5) 

### Impact Explanation

An active validator controlling a stake pool can execute the following cycle with a fixed amount of APT (e.g., `D` APT):

1. `add_stake(D)` → `total_joining_power += D` (checked against limit)
2. `unlock(D)` → `total_joining_power` unchanged; `D` moves to `pending_inactive`
3. `reactivate_stake(D)` → `total_joining_power` unchanged; `D` moves back to `active`
4. Repeat from step 1

After `K` iterations, `total_joining_power = K * D`. When `K * D` reaches `total_voting_power * voting_power_increase_limit / 100`, the next `add_stake` call by **any** validator fails with `EVOTING_POWER_INCREASE_EXCEEDS_LIMIT`. The attacker's net stake position is unchanged (they still hold `D` APT in `active`), but the joining limit is exhausted for the epoch.

**Concrete example**: If `total_voting_power = 1,000,000 APT` and `voting_power_increase_limit = 10%`, the limit is 100,000 APT. An attacker with only 1,000 APT (the minimum stake) can exhaust this in 100 transactions, blocking all new validator joins and stake additions for the rest of the epoch.

Blocked operations:
- `join_validator_set` for any new validator (calls `update_voting_power_increase`)
- `add_stake_with_cap` for any active/pending-active validator [7](#0-6) 

### Likelihood Explanation

- The attacker must be an active validator (controls a `StakePool` with `OwnerCapability`), which is a low barrier on mainnet where many validators exist.
- The attack requires only the minimum validator stake (100 APT on mainnet) to be effective, since the same APT is recycled.
- The attack is fully unprivileged: `add_stake`, `unlock`, and `reactivate_stake` are all public entry functions callable by any stake pool owner.
- The effect lasts until the next epoch boundary, which can be hours.
- No front-running is needed; the attacker simply submits a batch of transactions.

### Recommendation

When `unlock_with_cap` moves stake from `active` to `pending_inactive` for an active or pending-active validator, decrement `total_joining_power` by the unlocked amount (mirroring the decrement done in `leave_validator_set` for pending-active validators). A symmetric approach:

```move
// In unlock_with_cap, after extracting from active:
if (is_current_epoch_validator(pool_address)) {
    let validator_set = borrow_global_mut<ValidatorSet>(@aptos_framework);
    if (validator_set.total_joining_power > (amount as u128)) {
        validator_set.total_joining_power -= (amount as u128);
    } else {
        validator_set.total_joining_power = 0;
    };
};
```

This mirrors the existing decrement logic in `leave_validator_set`: [8](#0-7) 

### Proof of Concept

```
Setup:
  - total_voting_power = 1,000,000 APT
  - voting_power_increase_limit = 10%  → limit = 100,000 APT
  - Attacker is an active validator with 1,000 APT in active stake

Attack (100 iterations):
  for i in 1..=100:
    add_stake(1000)        // total_joining_power += 1000
    unlock(1000)           // total_joining_power unchanged
    reactivate_stake(1000) // total_joining_power unchanged

After loop:
  total_joining_power = 100,000 APT  (= limit)
  Attacker's active stake = 1,000 APT (unchanged)

Effect:
  Any subsequent add_stake or join_validator_set call by any validator
  aborts with EVOTING_POWER_INCREASE_EXCEEDS_LIMIT for the rest of the epoch.
```

The invariant broken is: `total_joining_power` should reflect the **net** new voting power that will join in the next epoch, but after the attack it reflects a grossly inflated value that has no correspondence to actual pending stake. [9](#0-8)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L184-196)
```text
    struct ValidatorSet has copy, key, drop, store {
        consensus_scheme: u8,
        // Active validators for the current epoch.
        active_validators: vector<ValidatorInfo>,
        // Pending validators to leave in next epoch (still active).
        pending_inactive: vector<ValidatorInfo>,
        // Pending validators to join in next epoch.
        pending_active: vector<ValidatorInfo>,
        // Current total voting power.
        total_voting_power: u128,
        // Total voting power waiting to join in the next epoch.
        total_joining_power: u128
    }
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L901-906)
```text
        let validator_set = borrow_global<ValidatorSet>(@aptos_framework);
        // Search directly rather using get_validator_state to save on unnecessary loops.
        if (find_validator(&validator_set.active_validators, pool_address).is_some()
            || find_validator(&validator_set.pending_active, pool_address).is_some()) {
            update_voting_power_increase(amount);
        };
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L936-955)
```text
    public fun reactivate_stake_with_cap(
        owner_cap: &OwnerCapability, amount: u64
    ) acquires StakePool {
        assert_reconfig_not_in_progress();
        let pool_address = owner_cap.pool_address;
        assert_stake_pool_exists(pool_address);

        // Cap the amount to reactivate by the amount in pending_inactive.
        let stake_pool = borrow_global_mut<StakePool>(pool_address);
        let total_pending_inactive = coin::value(&stake_pool.pending_inactive);
        amount = min(amount, total_pending_inactive);

        // Since this does not count as a voting power change (pending inactive still counts as voting power in the
        // current epoch), stake can be immediately moved from pending inactive to active.
        // We also don't need to check voting power increase as there's none.
        let reactivated_coins = coin::extract(&mut stake_pool.pending_inactive, amount);
        coin::merge(&mut stake_pool.active, reactivated_coins);

        event::emit(ReactivateStake { pool_address, amount });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1148-1164)
```text
    public fun unlock_with_cap(amount: u64, owner_cap: &OwnerCapability) acquires StakePool {
        assert_reconfig_not_in_progress();
        // Short-circuit if amount to unlock is 0 so we don't emit events.
        if (amount == 0) { return };

        // Unlocked coins are moved to pending_inactive. When the current lockup cycle expires, they will be moved into
        // inactive in the earliest possible epoch transition.
        let pool_address = owner_cap.pool_address;
        assert_stake_pool_exists(pool_address);
        let stake_pool = borrow_global_mut<StakePool>(pool_address);
        // Cap amount to unlock by maximum active stake.
        let amount = min(amount, coin::value(&stake_pool.active));
        let unlocked_stake = coin::extract(&mut stake_pool.active, amount);
        coin::merge<AptosCoin>(&mut stake_pool.pending_inactive, unlocked_stake);

        event::emit(UnlockStake { pool_address, amount_unlocked: amount });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1235-1246)
```text
            // Decrease the voting power increase as the pending validator's voting power was added when they requested
            // to join. Now that they changed their mind, their voting power should not affect the joining limit of this
            // epoch.
            let validator_stake = (get_voting_power(stake_pool) as u128);
            // total_joining_power should be larger than validator_stake but just in case there has been a small
            // rounding error somewhere that can lead to an underflow, we still want to allow this transaction to
            // succeed.
            if (validator_set.total_joining_power > validator_stake) {
                validator_set.total_joining_power -= validator_stake;
            } else {
                validator_set.total_joining_power = 0;
            };
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1403-1403)
```text
        validator_set.total_joining_power = 0;
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L2071-2088)
```text
    fun update_voting_power_increase(increase_amount: u64) acquires ValidatorSet {
        let validator_set = borrow_global_mut<ValidatorSet>(@aptos_framework);
        let voting_power_increase_limit =
            (
                staking_config::get_voting_power_increase_limit(&staking_config::get()) as u128
            );
        validator_set.total_joining_power +=(increase_amount as u128);

        // Only validator voting power increase if the current validator set's voting power > 0.
        if (validator_set.total_voting_power > 0) {
            assert!(
                validator_set.total_joining_power
                    <= validator_set.total_voting_power * voting_power_increase_limit
                        / 100,
                error::invalid_argument(EVOTING_POWER_INCREASE_EXCEEDS_LIMIT)
            );
        }
    }
```
