### Title
Global commission cap change via `set_configs` misrates already-accrued pool rewards without snapshotting - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` already patches this exact bug class for **pool-scoped** commission caps: both `Call::set_commission` and `Call::set_commission_max` call `reward_pool.update_records(...)` to snapshot pending rewards at the *old* effective commission rate before the rate changes (see the fix documented in `prdoc/pr_12397.prdoc` and the "IMPORTANT" comments at [1](#0-0)  and [2](#0-1) ). However, the **global** cap, `GlobalMaxCommission`, is mutated by `Call::set_configs` with no equivalent snapshot, even though `Commission::current()` reads it live and bounds every pool's effective commission by it.

### Finding Description
`Commission::current()` computes the effective commission rate for reward-splitting purposes as `self.current.min(GlobalMaxCommission::<T>::get())`: [3](#0-2) 

This value feeds `RewardPool::update_records`/`current_reward_counter`, which retroactively rates **all reward balance accrued since the last snapshot** (`last_recorded_reward_counter`) at whatever `commission` is passed in at call time: [4](#0-3) 

`Call::set_configs`, gated only by `T::AdminOrigin`, directly overwrites `GlobalMaxCommission` via a generic `ConfigOp` macro with no per-pool bookkeeping at all: [5](#0-4) 

Unlike `set_commission`/`set_commission_max`, this call never touches any `RewardPools::<T>` entry, so no pool's `last_recorded_reward_counter`/`total_commission_pending` is snapshotted before the effective rate changes. The very next `update_records` call for any pool (triggered permissionlessly by `bond`, `unbond`, `claim_payout`, or `claim_commission`) will apply the *new* `GlobalMaxCommission`-bounded rate to the entire backlog of rewards that had actually accrued while the *old* rate was in effect — exactly the "configure_collection" pattern: a configuration update silently re-prices already-existing, unsettled state instead of only affecting rewards accrued going forward.

This is bidirectional:
- Lowering `GlobalMaxCommission` underpays the commission payee on the backlog (differential leaks to members), which is the same failure mode PR #12397 fixed for `set_commission_max`, just via the untouched global-cap path.
- Raising or removing `GlobalMaxCommission` (`ConfigOp::Set`/`ConfigOp::Remove`) after a pool's stored `commission.current` was previously capped down can cause the *uncapped* higher stored rate to suddenly apply retroactively to the whole unclaimed reward backlog, overcharging members in favor of the commission payee for a period during which the cap was actually in force.

### Impact Explanation
Reward and commission accounting is a core value-conservation invariant of `pallet-nomination-pools`: rewards accrued at a given historical commission rate must settle at that rate, and the pallet's own documentation calls this out as the anti-abuse guarantee ("commission is applied to rewards based on the current commission in effect at the time rewards are transferred", [6](#0-5) ). A missed snapshot on the `GlobalMaxCommission` update path breaks this guarantee for every pool simultaneously, misallocating funds between commission payees and pool members without any malicious actor, front-run, or leaked key involved — purely a bookkeeping gap in an otherwise-legitimate, already-partially-fixed configuration flow.

### Likelihood Explanation
`set_configs` is a normal, expected part of runtime parameter management (adjusting the network-wide commission ceiling), so this path will be exercised in the ordinary course of governance operation, not only under attack conditions. Any pool with pending, unclaimed rewards at the time of a `GlobalMaxCommission` change is affected the next time `update_records` runs, which happens on essentially every subsequent pool interaction (`bond`, `unbond`, `claim_payout`, `claim_commission`), making the misrating close to unavoidable once triggered.

### Recommendation
Before mutating `GlobalMaxCommission` in `set_configs`, iterate `RewardPools`/`BondedPools` (or defer the effect until the next natural snapshot boundary per pool) and call `update_records` for each pool using its currently-bounded `commission.current()`, mirroring the ordering already implemented for `set_commission` and `set_commission_max`. Given the potential unbounded iteration cost, an alternative is to record the block/rate at which `GlobalMaxCommission` changed and lazily reconcile per-pool backlogs across the boundary in `current_reward_counter` instead of applying a single flat rate to the whole unclaimed span.

### Proof of Concept
1. Pool `P` sets `commission.current = 90%`, payee `Q` (`Pools::set_commission`).
2. `GlobalMaxCommission` is `None` (unbounded), so `current()` returns 90%.
3. Governance/root calls `Pools::set_configs` with `global_max_commission = ConfigOp::Set(50%)`. No `update_records` is invoked for pool `P`; `RewardPool::last_recorded_reward_counter` is unchanged.
4. 100 units of reward accrue in pool `P`'s reward account with no intervening claim/bond (mirrors the pattern in `set_commission_max_snapshots_rewards_before_lowering_current`, [7](#0-6) , but via the global path instead of the pool-level path that was fixed).
5. Any member calls `claim_payout`, triggering `update_records` with `commission = current() = 50%` (capped). The entire 100 units accrued while the effective rate was 90% is now split 50/50 instead of 90/10 — payee `Q` is underpaid by 40 units, which instead leaks to the claiming member(s), even though the pool-level fix in `set_commission_max` explicitly prevents this exact leak for the pool-scoped max-commission call.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L153-159)
```rust
//! Implementation note: Commission is analogous to a separate member account of the pool, with its
//! own reward counter in the form of `current_pending_commission`.
//!
//! Crucially, commission is applied to rewards based on the current commission in effect at the
//! time rewards are transferred into the reward pool. This is to prevent the malicious behaviour of
//! changing the commission rate to a very high value after rewards are accumulated, and thus claim
//! an unexpectedly high chunk of the reward.
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1450-1470)
```rust
	fn current_reward_counter(
		&self,
		id: PoolId,
		bonded_points: BalanceOf<T>,
		commission: Perbill,
	) -> Result<(T::RewardCounter, BalanceOf<T>), Error<T>> {
		let balance = Self::current_balance(id);

		// Calculate the current payout balance. The first 3 values of this calculation added
		// together represent what the balance would be if no payouts were made. The
		// `last_recorded_total_payouts` is then subtracted from this value to cancel out previously
		// recorded payouts, leaving only the remaining payouts that have not been claimed.
		let current_payout_balance = balance
			.saturating_add(self.total_rewards_claimed)
			.saturating_add(self.total_commission_claimed)
			.saturating_sub(self.last_recorded_total_payouts);

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2979-2985)
```rust
			// IMPORTANT: make sure that everything up to this point is using the current commission
			// before it updates. Note that `try_update_current` could still fail at this point.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3018-3027)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L6990-7020)
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
```
