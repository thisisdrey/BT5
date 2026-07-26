### Title
`compute_simulated_validator_info` Omits `pending_inactive` Rewards from Simulated Voting Power, Causing DKG Target-Set Divergence from Sync Path — (`aptos-move/framework/aptos-framework/sources/stake.move`)

---

### Summary

`next_validator_consensus_infos_v2` pre-computes the next-epoch validator set for the DKG/async reconfiguration path by calling `compute_simulated_validator_info` for every candidate. That helper simulates what `update_stake_pool` will do, but it only adds rewards on the **active** stake (`cur_reward = calculate_rewards_amount(cur_active, ...)`). It never adds rewards on **`pending_inactive`** stake. The sync/governance path (`refresh_validator_set_in_place`) reads the stake pool *after* `update_stake_pool` has already credited both `rewards_active` and `rewards_pending_inactive`, so `get_voting_power` returns a strictly higher value for any validator that holds non-zero `pending_inactive` with an unexpired lockup. The two paths therefore produce different `voting_power` values for the same validator, and in the boundary case where the true power crosses `minimum_stake` only after adding `rewards_pending_inactive`, the DKG path silently drops the validator from the next-epoch active set while the sync path would have kept it.

---

### Finding Description

**Async path — `compute_simulated_validator_info` (lines 1574–1627):**

```move
let cur_reward =
    if (include_rewards && cur_active > 0) {
        calculate_rewards_amount(
            cur_active,                          // ← active stake only
            cur_perf.successful_proposals,
            cur_perf.successful_proposals + cur_perf.failed_proposals,
            rewards_rate,
            rewards_rate_denominator
        )
    } else { 0 };
let new_voting_power =
    cur_active
        + if (lockup_expired) { 0 } else { cur_pending_inactive }
        + cur_pending_active + cur_reward;       // ← rewards_pending_inactive absent
``` [1](#0-0) 

**Sync path — `update_stake_pool` (lines 1906–1921):**

```move
let rewards_active =
    distribute_rewards(&mut stake_pool.active, ...);
let rewards_pending_inactive =
    distribute_rewards(&mut stake_pool.pending_inactive, ...);  // ← credited here
``` [2](#0-1) 

After `update_stake_pool` runs, `get_voting_power` returns:

```
(cur_active + rewards_active + cur_pending_active)
  + (if lockup_expired { 0 } else { cur_pending_inactive + rewards_pending_inactive })
``` [3](#0-2) 

The delta between the two paths is exactly `rewards_pending_inactive` whenever `lockup_expired == false`.

**Where the divergence matters — `next_validator_consensus_infos_v2` (lines 1636–1699):**

```move
let (new_voting_power, new_validator_info) =
    compute_simulated_validator_info(..., candidate_in_current);
if (new_voting_power >= minimum_stake) {          // ← uses underestimated power
    new_active_validators.push_back(new_validator_info);
``` [4](#0-3) 

The result is stored as `PrecomputedValidatorSet` and consumed unconditionally by `on_new_epoch` on the DKG path:

```move
let PrecomputedValidatorSet { validator_set: precomputed, is_liveness_fallback } =
    move_from<PrecomputedValidatorSet>(@aptos_framework);
*validator_set = precomputed;
``` [5](#0-4) 

The sync path (`refresh_validator_set_in_place`) reads the stake pool **after** `update_stake_pool` has already applied `rewards_pending_inactive`, so it would include the validator. The two paths are mutually exclusive, meaning the DKG path's incorrect exclusion is final. [6](#0-5) 

---

### Impact Explanation

1. **Incorrect `voting_power` in the DKG target set**: Every active validator with `pending_inactive > 0` and an unexpired lockup has its simulated voting power underestimated by `rewards_pending_inactive`. This propagates into `total_voting_power` of the `PrecomputedValidatorSet`, skewing the DKG threshold weights.

2. **Incorrect validator exclusion (boundary case)**: If a validator's true next-epoch voting power satisfies `minimum_stake` only after adding `rewards_pending_inactive` — i.e., `cur_active + cur_pending_active + cur_pending_inactive < minimum_stake` but `+ rewards_pending_inactive >= minimum_stake` — the DKG path drops the validator from the next-epoch active set. The validator loses one full epoch of staking rewards (APT) and is absent from consensus for that epoch, constituting a direct, permanent loss of staking balance for an unprivileged on-chain actor.

3. **Committed invalid state**: The `PrecomputedValidatorSet` written to chain is the authoritative source for `on_new_epoch`; no on-chain mechanism corrects it before it is consumed.

---

### Likelihood Explanation

The trigger is any active validator that calls `unlock` (moving stake to `pending_inactive`) while their lockup has not yet expired — a routine, unprivileged operation. The boundary-case exclusion requires the validator's voting power to be within `rewards_pending_inactive` of `minimum_stake`. With a 7 %/year reward rate and ~2-hour epochs, `rewards_pending_inactive ≈ 0.0016 % × pending_inactive` per epoch. For a validator holding the minimum stake (1 M APT on mainnet) entirely in `pending_inactive`, the window is ~16 APT. This is narrow but non-zero, and the voting-power underestimation for all such validators is unconditional.

---

### Recommendation

In `compute_simulated_validator_info`, add the simulated reward on `pending_inactive` when the lockup has not expired, mirroring what `update_stake_pool` does:

```move
let cur_reward_pending_inactive =
    if (include_rewards && !lockup_expired && cur_pending_inactive > 0) {
        calculate_rewards_amount(
            cur_pending_inactive,
            cur_perf.successful_proposals,
            cur_perf.successful_proposals + cur_perf.failed_proposals,
            rewards_rate,
            rewards_rate_denominator
        )
    } else { 0 };

let new_voting_power =
    cur_active
        + if (lockup_expired) { 0 } else { cur_pending_inactive + cur_reward_pending_inactive }
        + cur_pending_active + cur_reward;
```

A property-based test should assert that for every active validator, `compute_simulated_validator_info` returns the same voting power as `get_voting_power` called after a dry-run of `update_stake_pool`.

---

### Proof of Concept

1. Validator V has `active = 900,000 APT`, `pending_inactive = 100,000 APT`, `pending_active = 0`, lockup not expired. `minimum_stake = 1,000,000 APT`. Rewards rate = 1 %/epoch.

2. `rewards_pending_inactive = 100,000 × 0.01 = 1,000 APT`.

3. **Async path** (`compute_simulated_validator_info`):
   - `cur_reward = 900,000 × 0.01 = 9,000 APT`
   - `new_voting_power = 900,000 + 100,000 + 0 + 9,000 = 1,009,000 APT` ✓ (included)
   
   *(In this example V is included. To hit the exclusion boundary, set `active = 890,000`, `pending_inactive = 100,000`:)*
   - `new_voting_power = 890,000 + 100,000 + 8,900 = 998,900 APT < 1,000,000` → **excluded**

4. **Sync path** (after `update_stake_pool`):
   - `active` becomes `890,000 + 8,900 = 898,900 APT`
   - `pending_inactive` becomes `100,000 + 1,000 = 101,000 APT`
   - `get_voting_power = 898,900 + 101,000 = 999,900 APT` — still below minimum in this example.

   *(Adjust: `active = 891,000`, `pending_inactive = 100,000`:)*
   - Async: `891,000 + 100,000 + 8,910 = 999,910 < 1,000,000` → **excluded**
   - Sync: `(891,000+8,910) + (100,000+1,000) = 1,000,910 >= 1,000,000` → **included**

5. V is absent from the `PrecomputedValidatorSet`. `on_new_epoch` consumes it. V is not in the next epoch's active set, forfeiting one epoch of staking rewards permanently.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1387-1389)
```text
            let PrecomputedValidatorSet { validator_set: precomputed, is_liveness_fallback } =
                move_from<PrecomputedValidatorSet>(@aptos_framework);
            *validator_set = precomputed;
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1490-1526)
```text
    fun refresh_validator_set_in_place(
        validator_set: &mut ValidatorSet,
        config: &staking_config::StakingConfig,
    ): Option<ValidatorSetLivenessFallback> acquires StakePool, ValidatorConfig {
        // Update active validator set so that network address/public key change takes effect.
        // Moreover, recalculate the total voting power, and deactivate the validator whose
        // voting power is less than the minimum required stake.
        let next_epoch_validators = vector::empty();
        let (minimum_stake, _) = staking_config::get_required_stake(config);
        let vlen = validator_set.active_validators.length();
        let total_voting_power = 0;
        let i = 0;
        while ({
            spec {
                invariant spec_validators_are_initialized(next_epoch_validators);
                invariant i <= vlen;
            };
            i < vlen
        }) {
            let old_validator_info = validator_set.active_validators.borrow_mut(i);
            let pool_address = old_validator_info.addr;
            let validator_config = borrow_global<ValidatorConfig>(pool_address);
            let stake_pool = borrow_global<StakePool>(pool_address);
            let new_validator_info =
                generate_validator_info(pool_address, stake_pool, *validator_config);

            // A validator needs at least the min stake required to join the validator set.
            if (new_validator_info.voting_power >= minimum_stake) {
                spec {
                    assume total_voting_power + new_validator_info.voting_power
                        <= MAX_U128;
                };
                total_voting_power +=(new_validator_info.voting_power as u128);
                next_epoch_validators.push_back(new_validator_info);
            };
            i += 1;
        };
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1588-1620)
```text
        let cur_reward =
            if (include_rewards && cur_active > 0) {
                spec {
                    assert candidate.config.validator_index
                        < len(validator_perf.validators);
                };
                let cur_perf =
                    validator_perf.validators.borrow(candidate.config.validator_index);
                spec {
                    assume cur_perf.successful_proposals + cur_perf.failed_proposals
                        <= MAX_U64;
                };
                calculate_rewards_amount(
                    cur_active,
                    cur_perf.successful_proposals,
                    cur_perf.successful_proposals + cur_perf.failed_proposals,
                    rewards_rate,
                    rewards_rate_denominator
                )
            } else { 0 };
        let lockup_expired = get_reconfig_start_time_secs()
            >= stake_pool.locked_until_secs;
        spec {
            assume cur_active + cur_pending_active + cur_reward <= MAX_U64;
            assume cur_active + cur_pending_inactive + cur_pending_active + cur_reward
                <= MAX_U64;
        };
        let new_voting_power =
            cur_active
                + if (lockup_expired) { 0 }
                else {
                    cur_pending_inactive
                } + cur_pending_active + cur_reward;
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1681-1697)
```text
            let (new_voting_power, new_validator_info) =
                compute_simulated_validator_info(
                    candidate,
                    validator_perf,
                    rewards_rate,
                    rewards_rate_denominator,
                    num_new_actives,
                    candidate_in_current
                );
            if (new_voting_power >= minimum_stake) {
                spec {
                    assume new_total_power + new_voting_power <= MAX_U128;
                };
                new_total_power +=(new_voting_power as u128);
                new_active_validators.push_back(new_validator_info);
                num_new_actives += 1;
            };
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1906-1921)
```text
        let rewards_active =
            distribute_rewards(
                &mut stake_pool.active,
                num_successful_proposals,
                num_total_proposals,
                rewards_rate,
                rewards_rate_denominator
            );
        let rewards_pending_inactive =
            distribute_rewards(
                &mut stake_pool.pending_inactive,
                num_successful_proposals,
                num_total_proposals,
                rewards_rate,
                rewards_rate_denominator
            );
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L2060-2069)
```text
    fun get_voting_power(stake_pool: &StakePool): u64 {
        let value_pending_active = coin::value(&stake_pool.pending_active);
        let value_active = coin::value(&stake_pool.active);
        let value_pending_inactive = coin::value(&stake_pool.pending_inactive);
        spec {
            assume value_pending_active + value_active + value_pending_inactive
                <= MAX_U64;
        };
        value_pending_active + value_active + value_pending_inactive
    }
```
