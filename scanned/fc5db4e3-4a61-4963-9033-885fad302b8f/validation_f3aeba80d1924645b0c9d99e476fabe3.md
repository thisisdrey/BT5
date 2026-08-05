## Analysis

The external Popcorn `Vault` bug reduces to a single broken invariant: **value accrued at one rate gets settled at a different, later rate because no snapshot was taken before the rate/ratio changed**, silently transferring value away from the intended recipient (the fee recipient) to the wrong party (existing shareholders).

The Polkadot SDK has an almost line-for-line acknowledged instance of this exact bug class in `pallet-nomination-pools`, already fixed for one specific trigger (`set_commission_max`) via [1](#0-0) , but the same defect remains open for a second, more impactful trigger: lowering `GlobalMaxCommission` through `set_configs`.

### Title
Lowering `GlobalMaxCommission` re-rates already-accrued pool rewards at the new (lower) commission rate, misallocating commission owed to the payee to pool members - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Commission::current()` computes the *effective* commission rate as `min(pool.current, GlobalMaxCommission)` [2](#0-1) . When `GlobalMaxCommission` is lowered via `set_configs` [3](#0-2) , every pool whose current commission sits above the new cap sees its *effective* rate drop immediately, with no snapshot of any `RewardPool` beforehand. The next time `update_records`/`current_reward_counter` runs for such a pool, it re-rates the *entire* unclaimed balance accrued since the last snapshot — including the period when the old, higher commission applied — at the new lower rate, crediting the difference to members instead of the commission payee.

### Finding Description
`RewardPool::current_reward_counter` computes pending commission for the whole un-snapshotted period using a single, current commission value:
```rust
let new_pending_commission = commission * current_payout_balance;
let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);
``` [4](#0-3) 

This is correct only if the commission rate has not changed since `last_recorded_reward_counter` was set. The pallet already recognizes this and mitigates it for the pool-level `set_commission_max` path: it now calls `update_records` (i.e., snapshots the reward pool at the *old* commission) before calling `try_update_max`, which force-lowers `commission.current` [5](#0-4) , mirroring `set_commission`'s ordering [6](#0-5) . The regression test explicitly documents the invariant being protected [7](#0-6) .

However, `Commission::current()` also silently clamps to `GlobalMaxCommission` [2](#0-1) , and `set_configs` mutates `GlobalMaxCommission` directly with no iteration over `RewardPools`/`BondedPools` and no call to `update_records` for any pool:
```rust
config_op_exp!(GlobalMaxCommission::<T>, global_max_commission);
``` [8](#0-7) 

So for every pool with `commission.current > new GlobalMaxCommission`, `bonded_pool.commission.current()` returns the new, lower value starting the instant `set_configs` executes — but `RewardPool::last_recorded_reward_counter`/`last_recorded_total_payouts` were never snapshotted at the old (higher) rate. The very next `claim_payout`, `claim_commission`, `bond_extra`, `set_commission`, or `set_commission_max` on that pool will run `update_records`/`current_reward_counter` using the *new* lower rate over the *entire* un-snapshotted interval, understating the commission payee's entitlement and over-crediting members — exactly the invariant violation the PR-12397 fix targeted, just reached through a different call path that was not covered by that fix.

### Impact Explanation
This corrupts `RewardPool::total_commission_pending` (understated) and correspondingly the reward counters credited to members (overstated) for potentially every pool in the system simultaneously, since `GlobalMaxCommission` is a single global value affecting all pools whose current commission exceeds the new cap. Funds intended for the commission payee are permanently reallocated to ordinary pool members with no way to recover the differential — a concrete, non-reversible fund misallocation across pool-held value, matching the "conserve value and settle exactly once to the rightful beneficiary" pivot.

### Likelihood Explanation
No malicious actor, node, validator, or key compromise is required. The only precondition is a normal `AdminOrigin` action lowering `GlobalMaxCommission` (a routine, expected governance/config operation, not "admin abuse") while any pool has accrued but unclaimed rewards and a current commission above the new cap — a state that is easy to reach organically (rewards pending is the normal, common state; and pools with high commission near/above a soon-to-be-lowered cap are exactly the population the parameter change is meant to affect). The bug fires automatically on the very next reward-related extrinsic for such a pool; no attacker interaction is needed.

### Recommendation
Before writing the new `GlobalMaxCommission` in `set_configs`, iterate all `BondedPools`/`RewardPools` (or lazily but correctly account for it) and call `RewardPool::update_records` for every pool whose effective commission would be lowered by the change, exactly as already done for `set_commission_max`:
```rust
ConfigOp::Set(new_max) => {
    for (pool_id, mut bonded_pool) in BondedPools::<T>::iter() {
        if bonded_pool.commission.current() > new_max {
            if let Some(mut reward_pool) = RewardPools::<T>::get(pool_id) {
                let _ = reward_pool.update_records(
                    pool_id,
                    bonded_pool.points,
                    bonded_pool.commission.current(),
                );
                RewardPools::<T>::insert(pool_id, reward_pool);
            }
        }
    }
    GlobalMaxCommission::<T>::put(new_max);
},
```
Alternatively, restructure `Commission::current()` so that global-cap clamping is only applied at read/settlement points that already snapshot, rather than silently changing the "current" rate used retroactively over un-snapshotted history.

### Proof of Concept
1. Pool 1 sets `commission.current = 90%`, payee = `P` (`set_commission`, which snapshots at 90%).
2. `deposit_rewards(100)` accrues 100 units of unclaimed payout balance in the pool's reward account, with `last_recorded_reward_counter` still reflecting the pre-deposit state.
3. Root/`AdminOrigin` calls `set_configs` lowering `GlobalMaxCommission` to `20%` (no per-pool snapshot occurs, unlike `set_commission_max`).
4. Any account calls `claim_payout`/`claim_commission` for pool 1. `update_records` runs `current_reward_counter` with `commission = bonded_pool.commission.current() == 20%` (clamped by the new global max) over the *entire* 100 units accrued while the effective rate was still 90%.
5. Result: pending commission is computed as `20% * 100 = 20` instead of the correct `90 * 100 = 90` (the `set_commission_max`-fixed behavior), i.e. 70 units that should have gone to payee `P` are instead distributed to members — reproducing the exact scenario in `set_commission_max_snapshots_rewards_before_lowering_current` [7](#0-6)  but via the `set_configs`/`GlobalMaxCommission` path, which received no equivalent fix.

### Citations

**File:** prdoc/pr_12397.prdoc (L1-12)
```text
title: 'nomination-pools: snapshot rewards before `set_commission_max` lowers current commission'
doc:
- audience: Runtime Dev
  description: |-
    `set_commission_max` force-lowers `commission.current` (via `try_update_max`) when the new max
    is below the active rate, but did not first call `update_records`. Rewards accrued at the higher
    rate since the last snapshot were therefore re-rated at the new lower rate on the next
    `update_records`, crediting the differential `(old_current - new_max) * accrued` to members
    instead of the commission payee.

    The fix snapshots the reward pool at the current commission before the cut, mirroring the
    ordering already used in `set_commission`.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L843-850)
```rust
	/// Gets the pool's current commission, or returns Perbill::zero if none is set.
	/// Bounded to global max if current is greater than `GlobalMaxCommission`.
	fn current(&self) -> Perbill {
		self.current
			.as_ref()
			.map_or(Perbill::zero(), |(c, _)| *c)
			.min(GlobalMaxCommission::<T>::get().unwrap_or(Bounded::max_value()))
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1467-1470)
```rust
		// Split the `current_payout_balance` into claimable rewards and claimable commission
		// according to the current commission rate.
		let new_pending_commission = commission * current_payout_balance;
		let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2738-2778)
```rust
		#[pallet::call_index(11)]
		#[pallet::weight(T::WeightInfo::set_configs())]
		pub fn set_configs(
			origin: OriginFor<T>,
			min_join_bond: ConfigOp<BalanceOf<T>>,
			min_create_bond: ConfigOp<BalanceOf<T>>,
			max_pools: ConfigOp<u32>,
			max_members: ConfigOp<u32>,
			max_members_per_pool: ConfigOp<u32>,
			global_max_commission: ConfigOp<Perbill>,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin(origin)?;

			macro_rules! config_op_exp {
				($storage:ty, $op:ident) => {
					match $op {
						ConfigOp::Noop => (),
						ConfigOp::Set(v) => <$storage>::put(v),
						ConfigOp::Remove => <$storage>::kill(),
					}
				};
			}

			config_op_exp!(MinJoinBond::<T>, min_join_bond);
			config_op_exp!(MinCreateBond::<T>, min_create_bond);
			config_op_exp!(MaxPools::<T>, max_pools);
			config_op_exp!(MaxPoolMembers::<T>, max_members);
			config_op_exp!(MaxPoolMembersPerPool::<T>, max_members_per_pool);
			config_op_exp!(GlobalMaxCommission::<T>, global_max_commission);

			Self::deposit_event(Event::<T>::GlobalParamsUpdated {
				min_join_bond: MinJoinBond::<T>::get(),
				min_create_bond: MinCreateBond::<T>::get(),
				max_pools: MaxPools::<T>::get(),
				max_members: MaxPoolMembers::<T>::get(),
				max_members_per_pool: MaxPoolMembersPerPool::<T>::get(),
				global_max_commission: GlobalMaxCommission::<T>::get(),
			});

			Ok(())
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2977-2988)
```rust
			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: make sure that everything up to this point is using the current commission
			// before it updates. Note that `try_update_current` could still fail at this point.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);

			bonded_pool.commission.try_update_current(&new_commission)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3016-3029)
```rust
			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: snapshot rewards accrued at the current commission before `try_update_max`
			// can force-lower it. Otherwise rewards accrued since the last snapshot would be
			// re-rated at the new (lower) rate and the differential credited to members instead
			// of the commission payee. Mirrors the ordering in `set_commission`.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);

			bonded_pool.commission.try_update_max(pool_id, max_commission)?;
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L6990-7031)
```rust
	#[test]
	fn set_commission_max_snapshots_rewards_before_lowering_current() {
		// `set_commission_max` force-lowers `current` when the new max is below it. Rewards that
		// accrued at the higher rate since the last snapshot must stay owed to the payee at that
		// higher rate, not be re-rated at the new lower rate and leaked to members.
		ExtBuilder::default().build_and_execute(|| {
			let pool_id = 1;
			let payee = 900;
			let _ = Currency::set_balance(&payee, 5);

			// GIVEN: commission is 50% (this snapshots the still-empty reward pool)...
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(900),
				pool_id,
				Some((Perbill::from_percent(50), payee))
			));
			// ...and 100 of rewards accrue with no intervening snapshot (no claim/bond happens).
			deposit_rewards(100);
			assert_eq!(RewardPool::<Runtime>::current_balance(pool_id), 100);
			assert_eq!(RewardPools::<Runtime>::get(pool_id).unwrap().total_commission_pending, 0);

			// WHEN: root force-lowers max commission to 20%, cutting `current` from 50% to 20%.
			assert_ok!(Pools::set_commission_max(
				RuntimeOrigin::signed(900),
				pool_id,
				Perbill::from_percent(20)
			));

			// THEN: the 100 that accrued at 50% was snapshotted before the cut, so 50 is owed to
			// the payee. Without the pre-cut snapshot this would be 20% * 100 = 20.
			assert_eq!(RewardPools::<Runtime>::get(pool_id).unwrap().total_commission_pending, 50);

			// AND: claiming commission pays the payee the pre-cut 50, not the post-cut 20.
			let _ = pool_events_since_last_call();
			assert_ok!(Pools::claim_commission(RuntimeOrigin::signed(payee), pool_id));
			assert_eq!(
				pool_events_since_last_call(),
				vec![Event::PoolCommissionClaimed { pool_id, commission: 50 }]
			);
			assert_eq!(Currency::free_balance(&payee), 5 + 50);
			assert_eq!(RewardPools::<Runtime>::get(pool_id).unwrap().total_commission_claimed, 50);
		})
```
