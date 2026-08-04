The exact bug class from the external report (a global rate parameter that gets applied retroactively to an entire un-snapshotted accrual window, misallocating funds between two parties) already caused one fix in this repo — commit/PR captured in `prdoc/pr_12397.prdoc` fixed `set_commission_max`. But the *same* unguarded pattern still exists for the pallet-wide `GlobalMaxCommission` parameter, which is never snapshotted into any pool's reward ledger before it changes.

### Title
Changing `GlobalMaxCommission` via `set_configs` retroactively re-rates unsnapshotted pool rewards, misallocating commission between payee and members - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` bounds every pool's effective commission by the pallet-wide `GlobalMaxCommission` storage value at the moment `Commission::current()` is evaluated [1](#0-0) . `GlobalMaxCommission` itself can be updated at any time via `set_configs` [2](#0-1) , but this call never invokes `RewardPool::update_records` for any existing pool before the change takes effect. Consequently, when the reward ledger of a pool is next snapshotted (via `set_commission`, `set_commission_max`, `claim_commission`, `do_reward_payout`, etc.), the *entire* un-snapshotted balance accrued since the previous snapshot — some of it accrued while the old `GlobalMaxCommission` bound was in effect — is retroactively split using the *new* bound in `current_reward_counter` [3](#0-2) .

### Finding Description
`update_records`/`current_reward_counter` compute `new_pending_commission = commission * current_payout_balance` over the whole unsnapshotted accrual window using a single point-in-time `commission` value [4](#0-3) . The `commission` argument passed in every call site is `bonded_pool.commission.current()` [5](#0-4) , and `current()` unconditionally clamps the pool's nominal commission to the *live* `GlobalMaxCommission` value read at call time [1](#0-0) .

This is exactly the invariant the maintainers already recognized as broken for `set_commission_max` and fixed by snapshotting the reward pool with the *old* commission before applying the cut, as documented in `prdoc/pr_12397.prdoc` [6](#0-5) . The doc-comment for `update_records` even states the rule generally: it "MUST be called whenever ... the pools commission is updated" [7](#0-6) , but `GlobalMaxCommission`, which is also an input to the effective commission via `current()`, is changed by `set_configs` without ever iterating `RewardPools` to call `update_records` for pools whose current commission exceeds the new bound [8](#0-7) .

Net effect: any un-claimed/un-snapshotted reward balance that accrued while the old `GlobalMaxCommission` bound applied gets re-rated at the new bound the next time any commission-related extrinsic runs `update_records`, silently shifting funds between the commission payee and pool members compared to what should have been owed at each point in time — the same "wrong beneficiary or amount" defect pattern the external report and the already-fixed `set_commission_max` bug describe, just via a different trigger (`GlobalMaxCommission` rather than a per-pool `max`).

### Impact Explanation
Every open pool whose nominal commission is at or above `GlobalMaxCommission` is affected simultaneously by a single storage write, and there is no way to remediate already-accrued-but-unsnapshotted balances after the fact since the snapshot always uses only the currently-cached commission. If `GlobalMaxCommission` is lowered, members are overpaid at the commission payee's expense (funds that should have gone to the payee leak to members); if raised, payees can retroactively capture a share of rewards that had already legitimately accrued to members at the lower bound. This is a runtime bug compromising intended reward-accounting behavior across all pools bound by the parameter, matching the "wrong beneficiary or amount" impact class in scope.

### Likelihood Explanation
The trigger (`set_configs` changing `GlobalMaxCommission`) is a normal, expected pallet-configuration operation — not a governance abuse scenario, since lowering/raising the global cap is a documented, intended use of the parameter (analogous to `reserveFactor` updates in the source report, which are also routine protocol parameter changes). No malicious actor, relayer, or validator is required; the defect fires automatically the next time `update_records` runs for any affected pool, which happens on ordinary member/payee actions (`claim_payout`, `claim_commission`, `set_commission`, bonding/unbonding).

### Recommendation
Before writing a lowered `GlobalMaxCommission` in `set_configs`, iterate `RewardPools`/`BondedPools` (or defer the effective change via a similar "snapshot-then-apply" pattern used for `set_commission_max`) to call `update_records` for every pool whose current commission would be affected, so all unsnapshotted rewards are settled at the commission rate in effect at each point in time. Alternatively, decouple `current()`'s dependency on live `GlobalMaxCommission` reads from `current_reward_counter`'s single-shot re-rating, e.g. by tracking a `lastEffectiveCommission` per pool the way `lastReserveFactor` was recommended in the source report.

### Proof of Concept
1. Create pool `1` with commission `90%`, `GlobalMaxCommission = None` (uncapped) so `current()` returns `90%`.
2. Deposit rewards `100`; do not claim or trigger any commission-related call (no `update_records`), so nothing is snapshotted yet.
3. Governance (via `set_configs`) sets `GlobalMaxCommission = Some(10%)`.
4. Any subsequent action that triggers `update_records` (e.g. `claim_payout`) re-rates the entire un-snapshotted `100` balance at `10%` instead of `90%`: `new_pending_commission = 10% * 100 = 10` (per `current_reward_counter` [9](#0-8) ), crediting `80` extra to members that should have gone to the commission payee under the `90%` rate that was actually in effect while the `100` accrued — mirroring the already-demonstrated `set_commission_max_snapshots_rewards_before_lowering_current` test scenario [10](#0-9)  but via the unguarded `GlobalMaxCommission` path instead of the now-fixed `set_commission_max`.

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1402-1407)
```rust
	/// Update the recorded values of the reward pool.
	///
	/// This function MUST be called whenever the points in the bonded pool change, AND whenever the
	/// the pools commission is updated. The reason for the former is that a change in pool points
	/// will alter the share of the reward balance among pool members, and the reason for the latter
	/// is that a change in commission will alter the share of the reward balance among the pool.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1408-1470)
```rust
	fn update_records(
		&mut self,
		id: PoolId,
		bonded_points: BalanceOf<T>,
		commission: Perbill,
	) -> Result<(), Error<T>> {
		let balance = Self::current_balance(id);

		let (current_reward_counter, new_pending_commission) =
			self.current_reward_counter(id, bonded_points, commission)?;

		// Store the reward counter at the time of this update. This is used in subsequent calls to
		// `current_reward_counter`, whereby newly pending rewards (in points) are added to this
		// value.
		self.last_recorded_reward_counter = current_reward_counter;

		// Add any new pending commission that has been calculated from `current_reward_counter` to
		// determine the total pending commission at the time of this update.
		self.total_commission_pending =
			self.total_commission_pending.saturating_add(new_pending_commission);

		// Total payouts are essentially the entire historical balance of the reward pool, equating
		// to the current balance + the total rewards that have left the pool + the total commission
		// that has left the pool.
		let last_recorded_total_payouts = balance
			.checked_add(&self.total_rewards_claimed.saturating_add(self.total_commission_claimed))
			.ok_or(Error::<T>::OverflowRisk)?;

		// Store the total payouts at the time of this update.
		//
		// An increase in ED could cause `last_recorded_total_payouts` to decrease but we should not
		// allow that to happen since an already paid out reward cannot decrease. The reward account
		// might go in deficit temporarily in this exceptional case but it will be corrected once
		// new rewards are added to the pool.
		self.last_recorded_total_payouts =
			self.last_recorded_total_payouts.max(last_recorded_total_payouts);

		Ok(())
	}

	/// Get the current reward counter, based on the given `bonded_points` being the state of the
	/// bonded pool at this time.
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2737-2777)
```rust
		/// * `global_max_commission` - Set [`GlobalMaxCommission`].
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2977-2985)
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
```

**File:** prdoc/pr_12397.prdoc (L1-13)
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
crates:
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L6990-7032)
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
	}
```
