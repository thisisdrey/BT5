### Title
Nomination pool `unbond`/`withdraw_unbonded` can dissolve member `points` without the matching bonded balance actually being released, permanently trapping member funds - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The external report's core defect is a ledger that is advanced (`principalSharesDeposited`/`principalAssetsDeposited` incremented for `receiver`) without any matching real transfer of the underlying asset, so the internal accounting diverges from the actual token balance. The direct analog in this repository is `pallet-nomination-pools`, where a member's `points` ledger can be decremented (`dissolve`/`try_unbond`) in `unbond` while the actually staked/held balance is not released in lock-step, because the pool's era bookkeeping and `pallet-staking`'s era bookkeeping are tracked independently. This exact class of divergence was documented and had to be repaired via a one-off storage migration.

### Finding Description
`unbond()` computes `unbonding_balance` from the bonded pool's points/balance ratio, mutates `bonded_pool.points` via `dissolve()`, then issues points into an `UnbondPool` keyed by `unbond_era = T::StakeAdapter::bonding_duration() + T::StakeAdapter::current_era()`, and finally records this in the member via `member.try_unbond(...)`: [1](#0-0) 

The member's points are moved from `active_points` into `unbonding_eras` in `try_unbond`, independent of whether the underlying staking ledger actually finishes unbonding on the expected era boundary: [2](#0-1) 

Later, `withdraw_unbonded` dissolves the recorded `unlocked_points` from the corresponding `UnbondPool`/`sub_pools`, computing `balance_to_unbond`, then calls `T::StakeAdapter::member_withdraw` to actually release funds - but the amount released is capped by `transferable_balance`, which depends on `pallet-staking`'s own era clock: [3](#0-2) 

This is precisely the "accounting incremented/decremented without an actual matching transfer" pattern from the Tranche.sol report: the pool's `points`/`member.points` ledger (the analog of `principalSharesDeposited`) is advanced by `unbond`/`withdraw_unbonded` on the pool-pallet's own era timeline, while the actual bonded balance release is gated by `pallet-staking`'s independently tracked `CurrentEra`/`ActiveEra`. This is confirmed by the project's own fix record: a `CurrentEra` vs `ActiveEra` mismatch caused a pool member's `points` to be dissolved while the "held funds weren't released," requiring a one-time migration to reconcile actual held balance against the balance implied by points: [4](#0-3) 

The project later added a defensive `try-state` invariant enforcing `points >= stake` for bonded pools, which is a detection mechanism, not a fix of the underlying dual-clock divergence: [5](#0-4) 

The `withdraw_unbonded` code itself acknowledges the risk of exactly this scenario in its own comment, explicitly capping withdrawal to the currently transferable balance "in cases where the unbond pool does not update its balance (e.g. a bug in the slashing hook.)": [6](#0-5) 

### Impact Explanation
When the pool-side era bookkeeping and staking-side era bookkeeping diverge, a member's `points` (the pool-internal claim ledger) are consumed by `unbond`/`try_unbond` and later by `withdraw_unbonded`'s `dissolve`, but the real bonded/staked balance backing those points is not correspondingly released because `member_withdraw` clamps to `transferable_balance` computed from `pallet-staking`'s era. The member's on-chain ledger then shows zero (or reduced) points/claim while real value remains locked in the pool's bonded/staking account with no ledger entry pointing back to it — a permanent user-fund lock, matching the "permanent user-fund ... lock" impact class explicitly in scope. This required an ad-hoc governance-run migration to manually recompute and release trapped funds for the affected member, confirming actual fund loss/lock occurred in production before remediation.

### Likelihood Explanation
This does not require a malicious peer, validator, collator, or privileged actor — it is triggered purely by ordinary, permissionless use of the public `unbond` and `withdraw_unbonded` extrinsics under specific era-timing conditions (e.g., slashing-span interactions or era transitions occurring between the pool's recorded `unbond_era` and the staking pallet's own era advancement), as evidenced by the real-world occurrence that necessitated `pr_11018`'s one-time migration. The `try-state` check added in `pr_5465` only detects the invariant violation after the fact; it does not prevent the divergence from being introduced by normal `unbond`/`withdraw_unbonded` calls.

### Recommendation
Make the pool's `unbond_era`/`UnbondPool` bookkeeping strictly derive from the same authoritative era source used by `pallet-staking`'s `transferable_balance`/`member_withdraw` accounting (single source of truth for era progression), or make `withdraw_unbonded` atomic: only mutate `member.points`/`sub_pools` state after `T::StakeAdapter::member_withdraw` has confirmed the exact balance was released, and roll back (or partially re-credit points) if the released amount is less than what the dissolved points implied, rather than silently accepting a `min()`-clamped lower amount while still fully dissolving the points.

### Proof of Concept
A concrete reproducible PoC is not available from static repository analysis alone; the historical trigger conditions (`CurrentEra` vs `ActiveEra` mismatch during slashing-span handling) are runtime-timing dependent and are documented only at the level of the fix description in `prdoc/stable2512-3/pr_11018.prdoc`, not as a standalone test in this snapshot. The existence of the migration in `pr_11018` and the defensive `points >= stake` invariant in `pr_5465` are used here as repository evidence that this exact "ledger advances without matching real balance movement" condition has manifested via the public `unbond`/`withdraw_unbonded` call path in `substrate/frame/nomination-pools/src/lib.rs`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L626-660)
```rust
	/// Try and unbond `points_dissolved` from self, and in return mint `points_issued` into the
	/// corresponding `era`'s unlock schedule.
	///
	/// In the absence of slashing, these two points are always the same. In the presence of
	/// slashing, the value of points in different pools varies.
	///
	/// Returns `Ok(())` and updates `unbonding_eras` and `points` if success, `Err(_)` otherwise.
	fn try_unbond(
		&mut self,
		points_dissolved: BalanceOf<T>,
		points_issued: BalanceOf<T>,
		unbonding_era: EraIndex,
	) -> Result<(), Error<T>> {
		if let Some(new_points) = self.points.checked_sub(&points_dissolved) {
			match self.unbonding_eras.get_mut(&unbonding_era) {
				Some(already_unbonding_points) => {
					*already_unbonding_points =
						already_unbonding_points.saturating_add(points_issued)
				},
				None => self
					.unbonding_eras
					.try_insert(unbonding_era, points_issued)
					.map(|old| {
						if old.is_some() {
							defensive!("value checked to not exist in the map; qed");
						}
					})
					.map_err(|_| Error::<T>::MaxUnbondingLimit)?,
			}
			self.points = new_points;
			Ok(())
		} else {
			Err(Error::<T>::MinimumBondNotMet)
		}
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2290-2323)
```rust
			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

			// Note that we lazily create the unbonding pools here if they don't already exist
			let mut sub_pools = SubPoolsStorage::<T>::get(member.pool_id)
				.unwrap_or_default()
				.maybe_merge_pools(active_era);

			// Update the unbond pool associated with the current era with the unbonded funds. Note
			// that we lazily create the unbond pool if it does not yet exist.
			if !sub_pools.with_era.contains_key(&unbond_era) {
				sub_pools
					.with_era
					.try_insert(unbond_era, UnbondPool::default())
					// The above call to `maybe_merge_pools` should ensure there is
					// always enough space to insert.
					.defensive_map_err::<Error<T>, _>(|_| {
						DefensiveError::NotEnoughSpaceInUnbondPool.into()
					})?;
			}

			let points_unbonded = sub_pools
				.with_era
				.get_mut(&unbond_era)
				// The above check ensures the pool exists.
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?
				.issue(unbonding_balance);

			// Try and unbond in the member map.
			member.try_unbond(unbonding_points, points_unbonded, unbond_era)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2469-2505)
```rust
			let mut sum_unlocked_points: BalanceOf<T> = Zero::zero();
			let balance_to_unbond = withdrawn_points
				.iter()
				.fold(BalanceOf::<T>::zero(), |accumulator, (era, unlocked_points)| {
					sum_unlocked_points = sum_unlocked_points.saturating_add(*unlocked_points);
					if let Some(era_pool) = sub_pools.with_era.get_mut(era) {
						let balance_to_unbond = era_pool.dissolve(*unlocked_points);
						if era_pool.points.is_zero() {
							sub_pools.with_era.remove(era);
						}
						accumulator.saturating_add(balance_to_unbond)
					} else {
						// A pool does not belong to this era, so it must have been merged to the
						// era-less pool.
						accumulator.saturating_add(sub_pools.no_era.dissolve(*unlocked_points))
					}
				})
				// A call to this transaction may cause the pool's stash to get dusted. If this
				// happens before the last member has withdrawn, then all subsequent withdraws will
				// be 0. However the unbond pools do no get updated to reflect this. In the
				// aforementioned scenario, this check ensures we don't try to withdraw funds that
				// don't exist. This check is also defensive in cases where the unbond pool does not
				// update its balance (e.g. a bug in the slashing hook.) We gracefully proceed in
				// order to ensure members can leave the pool and it can be destroyed.
				.min(T::StakeAdapter::transferable_balance(
					Pool::from(bonded_pool.bonded_account()),
					Member::from(member_account.clone()),
				));

			// this can fail if the pool uses `DelegateStake` strategy and the member delegation
			// is not claimed yet. See `Call::migrate_delegation()`.
			T::StakeAdapter::member_withdraw(
				Member::from(member_account.clone()),
				Pool::from(bonded_pool.bonded_account()),
				balance_to_unbond,
				num_slashing_spans,
			)?;
```

**File:** prdoc/stable2512-3/pr_11018.prdoc (L1-15)
```text
title: '[Pool] Claim trapped balance via one-time migration'
doc:
- audience: Runtime User
  description: |-
    One-time migration to recover trapped balance for an affected pool member.
    A bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were
      dissolved but the held funds weren't released. This migration:
    - Applies any pending slash for the member first
    - Calculates trapped amount by checking actual held balance vs expected balance from points
    - Releases trapped funds if present
crates:
- name: pallet-nomination-pools
  bump: minor
- name: asset-hub-westend-runtime
  bump: patch
```

**File:** prdoc/1.16.0/pr_5465.prdoc (L1-10)
```text
title: try-state check invariant for nomination-pools (points >= stake)

doc:
  - audience: Runtime Dev
    description: |
      Adds a new try-state invariant to the nomination pools that checks that for each bonded pool, the pool's points can never be lower than its staked balance.

crates:
  - name: pallet-nomination-pools
    bump: minor
```
