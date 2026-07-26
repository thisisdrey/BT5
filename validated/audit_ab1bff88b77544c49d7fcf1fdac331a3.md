The claim requires careful tracing through the actual code. Let me walk through each step.

**Step 1: Does a pending_active validator end up in active_validators with active=0?**

In `on_new_epoch`, `update_stake_pool` is called only for `active_validators` and `pending_inactive` — never for `pending_active` validators. [1](#0-0) 

The pending_active validators are then appended to `active_validators` at line 1377 **without** having `update_stake_pool` called on them, so their `pending_active` stake is never moved to `active`. [2](#0-1) 

**Step 2: Does `refresh_validator_set_in_place` include them?**

`get_voting_power` returns `pending_active + active + pending_inactive`. For a validator with only pending_active=X, this equals X. If X ≥ minimum_stake, they pass the filter and are included in `next_epoch_validators`. [3](#0-2) 

**Step 3: Is a PendingTransactionFee entry created for them?**

After the validator set is refreshed, fee entries are created for **every** validator in `active_validators`, including the newly joined one, initialized to 0 via `create_unbounded_aggregator<u64>()`. [4](#0-3) 

**Step 4: Does the division by zero occur in epoch N+1?**

In epoch N+1's `on_new_epoch`, `update_stake_pool` is called for this validator. At that point, `stake_pool.active = 0` and `stake_pool.pending_inactive = 0` (pending_active has still not been moved). The fee entry exists (contains returns true), so the code enters the fee distribution block and evaluates:

```
fee_pending_inactive =
    (
        ((fee_octa as u128) * stake_pending_inactive
            / (stake_active + stake_pending_inactive)) as u64
    );
```

With `stake_active = 0` and `stake_pending_inactive = 0`, the denominator is `0 + 0 = 0`. Move aborts on integer division by zero **regardless of the numerator value** — even `0 / 0` aborts. There is no guard like `if (stake_active + stake_pending_inactive > 0)` before this division. [5](#0-4) 

The comment on `update_stake_pool` itself says "This function shouldn't abort." — the invariant is broken. [6](#0-5) 

**Conclusion: The vulnerability is real.**

---

### Title
Division-by-zero in `update_stake_pool` when a newly activated validator has `active=0` and `pending_inactive=0` permanently wedges `on_new_epoch` — (`aptos-move/framework/aptos-framework/sources/stake.move`)

### Summary
A validator that joins the set with only `pending_active` stake (active=0, pending_inactive=0) is appended to `active_validators` in epoch N without `update_stake_pool` being called on it. A `PendingTransactionFee` aggregator entry (value=0) is then created for it. In epoch N+1, `update_stake_pool` is called for this validator while `stake_pool.active` and `stake_pool.pending_inactive` are still both 0, causing an unconditional integer division by zero that aborts `on_new_epoch` permanently.

### Finding Description
`on_new_epoch` processes `active_validators` and `pending_inactive` validators through `update_stake_pool`, then appends `pending_active` validators directly to `active_validators` (line 1377) without calling `update_stake_pool` on them. This means a newly activated validator enters `active_validators` with `stake_pool.active = 0` and `stake_pool.pending_inactive = 0` — their stake remains in `pending_active`.

After the validator set is refreshed, `on_new_epoch` creates a `PendingTransactionFee` aggregator entry (initialized to 0) for every validator in `active_validators`, including the newly joined one (lines 1467–1476).

In epoch N+1, `update_stake_pool` is called for this validator. The fee distribution block (lines 1875–1892) checks `pending_fee_by_validator.contains(&validator_index)` — which is true — removes the entry, reads `fee_octa = 0`, then unconditionally evaluates:

```move
fee_pending_inactive =
    (((fee_octa as u128) * stake_pending_inactive
        / (stake_active + stake_pending_inactive)) as u64);
```

With `stake_active = 0` and `stake_pending_inactive = 0`, the denominator is `0`. Move's integer division aborts on any division by zero, including `0 / 0`. There is no guard protecting this path.

### Impact Explanation
`on_new_epoch` is called during every epoch transition (reconfiguration). If it aborts, the epoch transition is permanently wedged — no further epoch can ever complete. This is a total chain availability failure. All staking rewards, validator set changes, and governance actions that depend on epoch transitions are permanently frozen.

### Likelihood Explanation
Any validator operator can trigger this by calling `join_validator_set` with a stake pool that has only `pending_active` stake (i.e., they added stake while inactive and then joined). This is the normal validator onboarding flow. The `PendingTransactionFee` feature must be enabled (it is a production feature on mainnet). No privileged access is required — `join_validator_set` is a public entry function callable by any validator operator.

### Recommendation
Add a zero-denominator guard before the division in `update_stake_pool`:

```move
if (stake_active + stake_pending_inactive > 0) {
    fee_pending_inactive =
        (((fee_octa as u128) * stake_pending_inactive
            / (stake_active + stake_pending_inactive)) as u64);
    fee_active = fee_octa - fee_pending_inactive;
} else {
    fee_active = fee_octa; // all fees go to active (pending_active will become active)
};
```

Alternatively, skip fee distribution entirely when both are zero (the fees will be lost, but chain liveness is preserved).

### Proof of Concept
1. Enable `PendingTransactionFee` feature.
2. Create a validator with `pending_active` stake only (call `add_stake` while inactive, then `join_validator_set`).
3. Advance to epoch N: `on_new_epoch` appends the validator to `active_validators` and creates a fee entry with value 0.
4. Advance to epoch N+1: `on_new_epoch` calls `update_stake_pool` for this validator. `stake_pool.active = 0`, `stake_pool.pending_inactive = 0`, fee entry exists with `fee_octa = 0`. The division `0 / 0` aborts. `on_new_epoch` never completes again.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1346-1357)
```text
        // Process pending stake and distribute transaction fees and rewards for each currently active validator.
        validator_set.active_validators.for_each_ref(|validator| {
            let validator: &ValidatorInfo = validator;
            update_stake_pool(validator_perf, validator.addr, &config);
        });

        // Process pending stake and distribute transaction fees and rewards for each currently pending_inactive validator
        // (requested to leave but not removed yet).
        validator_set.pending_inactive.for_each_ref(|validator| {
            let validator: &ValidatorInfo = validator;
            update_stake_pool(validator_perf, validator.addr, &config);
        });
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1376-1377)
```text
        // Activate currently pending_active validators.
        append(&mut validator_set.active_validators, &mut validator_set.pending_active);
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1467-1476)
```text
        if (exists<PendingTransactionFee>(@aptos_framework)) {
            let pending_fee_by_validator =
                &mut borrow_global_mut<PendingTransactionFee>(@aptos_framework).pending_fee_by_validator;
            assert!(
                pending_fee_by_validator.is_empty(),
                error::internal(ETRANSACTION_FEE_NOT_FULLY_DISTRIBUTED)
            );
            validator_set.active_validators.for_each_ref(|v| pending_fee_by_validator.add(
                v.config.validator_index, aggregator_v2::create_unbounded_aggregator<u64>()
            ));
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1852-1852)
```text
    /// This function shouldn't abort.
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1875-1892)
```text
        if (exists<PendingTransactionFee>(@aptos_framework)) {
            let pending_fee_by_validator =
                &mut borrow_global_mut<PendingTransactionFee>(@aptos_framework).pending_fee_by_validator;
            if (pending_fee_by_validator.contains(&validator_index)) {
                let fee_octa = pending_fee_by_validator.remove(&validator_index).read();
                if (fee_octa > fee_limit) {
                    fee_octa = fee_limit;
                };
                let stake_active = (coin::value(&stake_pool.active) as u128);
                let stake_pending_inactive =
                    (coin::value(&stake_pool.pending_inactive) as u128);
                fee_pending_inactive =
                    (
                        ((fee_octa as u128) * stake_pending_inactive
                            / (stake_active + stake_pending_inactive)) as u64
                    );
                fee_active = fee_octa - fee_pending_inactive;
            }
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L2060-2068)
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
```
