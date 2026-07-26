### Title
Stale Commission Rate Applied to Pending-Inactive Rewards Allows Staker to Steal Operator Commission — (`File: aptos-move/framework/aptos-framework/sources/staking_contract.move`)

---

### Summary

`update_commision` in `staking_contract.move` allows a staker to change the commission percentage at any time. When pending-inactive stake has already accumulated rewards but the lockup has not yet expired, calling `update_commision` triggers `distribute_internal`, which returns early because no inactive funds are withdrawable. The commission rate is then overwritten. When the lockup later expires and `distribute` is called, `update_distribution_pool` applies the **new** (potentially 0%) commission rate to all rewards earned since the last pool update — including rewards earned entirely under the old rate. The operator's earned commission is silently zeroed out and the staker receives the full reward instead.

---

### Finding Description

`update_commision` (line 566) is callable by the staker with no restriction on how far the rate can be reduced:

```move
assert!(
    new_commission_percentage >= 0 && new_commission_percentage <= 100,
    error::invalid_argument(EINVALID_COMMISSION_PERCENTAGE)
);
``` [1](#0-0) 

It then calls `distribute_internal` before updating the rate:

```move
distribute_internal(staker_address, operator, staking_contract);
request_commission_internal(operator, staking_contract);
staking_contract.commission_percentage = new_commission_percentage;
``` [2](#0-1) 

`distribute_internal` reads `inactive + pending_inactive` and passes the sum to `stake::withdraw_with_cap`. When the lockup has not yet expired, `inactive = 0` and `withdraw_with_cap` returns zero coins, so the function exits immediately:

```move
let (_, inactive, _, pending_inactive) = stake::get_stake(pool_address);
let total_potential_withdrawable = inactive + pending_inactive;
let coins = stake::withdraw_with_cap(&staking_contract.owner_cap, total_potential_withdrawable);
let distribution_amount = coin::value(&coins);
if (distribution_amount == 0) {
    coin::destroy_zero(coins);
    return
};
``` [3](#0-2) 

No commission is charged on the pending-inactive rewards at this point. The commission rate is then overwritten to the new value.

Later, when the lockup expires and `distribute` (or any path that calls `distribute_internal`) is invoked, `update_distribution_pool` is called with the **current** (new) commission percentage:

```move
update_distribution_pool(
    distribution_pool,
    distribution_amount,
    operator,
    staking_contract.commission_percentage   // ← new rate, not the rate when rewards were earned
);
``` [4](#0-3) 

Inside `update_distribution_pool`, commission on the reward increment is computed as:

```move
let unpaid_commission =
    (current_worth - previous_worth) * commission_percentage / 100;
``` [5](#0-4) 

If `commission_percentage` was reduced to 0, `unpaid_commission = 0` and the operator receives nothing for rewards that accrued entirely under the original rate.

The commission percentage is never snapshotted at distribution-creation time. `add_distribution` also reads the live `staking_contract.commission_percentage`:

```move
update_distribution_pool(
    distribution_pool,
    total_distribution_amount,
    operator,
    staking_contract.commission_percentage
);
``` [6](#0-5) 

---

### Impact Explanation

The operator's commission on pending-inactive rewards is permanently zeroed. Those APT flow to the staker instead. This is a direct, quantifiable theft of the operator's staking balance. For a 1,000,000 APT unlock with 10% commission and 10% epoch rewards, the operator loses ~10,000 APT per lockup cycle the staker exploits this path.

---

### Likelihood Explanation

The staker is an unprivileged on-chain principal who can call `update_commision` at any time with no cooldown, no minimum floor, and no restriction on the magnitude of the decrease. The only prerequisite is having an active staking contract with pending-inactive stake — a normal operational state. The attack requires no special access, no governance vote, and no coordination.

---

### Recommendation

Snapshot the commission percentage at the time each distribution entry is created and use that snapshot — not the live `staking_contract.commission_percentage` — when computing `unpaid_commission` inside `update_distribution_pool`. One approach is to store the commission rate alongside each distribution entry (e.g., as a field in the distribution pool metadata) so that rewards earned under rate R are always charged at rate R regardless of subsequent rate changes.

---

### Proof of Concept

```
1. Staker creates staking contract with operator, commission = 10%.
2. Staker calls unlock_stake(1_000_000 APT).
   → add_distribution records staker in distribution_pool with 1_000_000 shares.
   → distribution_pool.total_coins = 1_000_000 (pending_inactive).
3. One epoch passes; pending_inactive grows to 1_100_000 APT (10% reward).
   Lockup has NOT expired yet.
4. Staker calls update_commision(operator, 0).
   → distribute_internal is called: inactive = 0, withdraw_with_cap returns 0 coins → early return.
   → request_commission_internal calculates commission on active rewards only (not pending_inactive).
   → staking_contract.commission_percentage is set to 0.
5. Lockup expires; pending_inactive (1_100_000 APT) becomes inactive.
6. Anyone calls distribute(staker, operator).
   → distribute_internal: distribution_amount = 1_100_000.
   → update_distribution_pool called with commission_percentage = 0.
   → unpaid_commission = (1_100_000 - 1_000_000) * 0 / 100 = 0.
   → Operator receives 0 APT commission.
   → Staker receives full 1_100_000 APT.

Expected: operator receives 10_000 APT (10% of 100_000 reward).
Actual:   operator receives 0 APT; staker steals 10_000 APT.
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L569-572)
```text
        assert!(
            new_commission_percentage >= 0 && new_commission_percentage <= 100,
            error::invalid_argument(EINVALID_COMMISSION_PERCENTAGE)
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L582-592)
```text
        distribute_internal(
            staker_address,
            operator,
            staking_contract,
        );
        request_commission_internal(
            operator,
            staking_contract,
        );
        let old_commission_percentage = staking_contract.commission_percentage;
        staking_contract.commission_percentage = new_commission_percentage;
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L868-878)
```text
        let (_, inactive, _, pending_inactive) = stake::get_stake(pool_address);
        let total_potential_withdrawable = inactive + pending_inactive;
        let coins =
            stake::withdraw_with_cap(
                &staking_contract.owner_cap, total_potential_withdrawable
            );
        let distribution_amount = coin::value(&coins);
        if (distribution_amount == 0) {
            coin::destroy_zero(coins);
            return
        };
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L881-886)
```text
        update_distribution_pool(
            distribution_pool,
            distribution_amount,
            operator,
            staking_contract.commission_percentage
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L947-952)
```text
        update_distribution_pool(
            distribution_pool,
            total_distribution_amount,
            operator,
            staking_contract.commission_percentage
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L1023-1024)
```text
                    let unpaid_commission =
                        (current_worth - previous_worth) * commission_percentage / 100;
```
