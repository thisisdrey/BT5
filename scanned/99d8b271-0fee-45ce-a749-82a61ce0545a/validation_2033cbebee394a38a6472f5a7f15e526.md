## Analysis

The Ajna bug's core invariant is: **a tracking index/entry is unconditionally removed from a set/mapping before or regardless of confirming that the corresponding value-transfer operation delivered the full amount**, causing the untransferred remainder to become permanently unclaimable because the only record of it was just deleted.

The closest verified local analog is `pallet-nomination-pools::withdraw_unbonded` in `substrate/frame/nomination-pools/src/lib.rs`.

### Title
Nomination pool member's unbonding record is unconditionally cleared before the transfer is capped by pool `transferable_balance`, permanently burning any shortfall - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`withdraw_unbonded` first removes matured unbonding entries from the member's tracking map via `member.withdraw_unlocked(active_era)`, and — if this empties the member's points — deletes `PoolMembers` for that account entirely, before the actual amount paid out is finally capped by `.min(T::StakeAdapter::transferable_balance(...))`. If the capped amount is less than the value implied by the dissolved points, the difference is lost with no remaining state to reclaim it, mirroring the Ajna `positionIndex.remove` freeze pattern (index/record removed unconditionally even on partial fulfillment).

### Finding Description
In `withdraw_unbonded` [1](#0-0) , `member.withdraw_unlocked(active_era)` unconditionally drains all unbonding-era entries whose era has matured from the member's `unbonding_eras` map [2](#0-1) , independent of whether the underlying balance can actually be delivered.

The routine then computes `balance_to_unbond` by dissolving the corresponding `UnbondPool` entries (which reduces `sub_pools` points/balance unconditionally as well) [3](#0-2) , and only at the end applies a defensive cap: `.min(T::StakeAdapter::transferable_balance(...))` [4](#0-3) . The code comment itself acknowledges the shortfall scenario: "the unbond pools do not get updated to reflect this... This check is also defensive in cases where the unbond pool does not update its balance (e.g. a bug in the slashing hook.) We gracefully proceed..." [5](#0-4) .

Crucially, by the time this cap is applied, the member's `unbonding_eras` entry and `sub_pools.with_era`/`no_era` points have already been irreversibly zeroed, and if `member.total_points()` is now zero the entire `PoolMembers` entry for that account is deleted [6](#0-5) . There is no remaining storage record of the un-paid difference — exactly the Ajna pattern where `positionIndex.remove(params_.fromIndex)` is executed unconditionally before confirming `moveQuoteToken` delivered the full requested amount, freezing the remainder because the tracking entry that referenced it is gone.

`transferable_balance` can legitimately be lower than the sum implied by the dissolved sub-pool balances for reasons within the `DelegateStake` adapter itself: it is defined as `agent_transferable_balance(pool).min(delegator_balance(member))` [7](#0-6) , which can differ from the pool-level accounting tracked in `UnbondPool`/`SubPools` (e.g., stash dusting affecting earlier withdrawers as explicitly called out in the code: "A call to this transaction may cause the pool's stash to get dusted. If this happens before the last member has withdrawn, then all subsequent withdraws will be 0.") [8](#0-7) .

### Impact Explanation
When the transferable-balance cap binds, an affected member's owed unbonded balance is permanently lost: their `unbonding_eras`/`sub_pools` accounting entry that was the only record of the amount owed has already been cleared, and (for a fully-unbonding member) their `PoolMembers` entry is deleted outright, removing any path to later reclaim the difference. This is a permanent, unbacked loss of user funds for the affected pool member, matching the "permanent user-fund lock"/"unbacked mint or unlock" impact class.

### Likelihood Explanation
This path is reachable via the public, permissionless `withdraw_unbonded` extrinsic [9](#0-8) ; no privileged actor is required. The condition is triggered by ordinary pool accounting divergence (e.g. dust-related shortfalls the code itself documents, or delegate-stake balance being less than the sub-pool computed value), not by any external attacker action, which is precisely why the developers added the `.min()` cap "to gracefully proceed" rather than reverting — the code accepts silent fund loss in favor of allowing exit, without any restitution/dust-tracking mechanism for the shortfall.

### Recommendation
Do not permanently clear `unbonding_eras`/`PoolMembers`/`sub_pools` state before confirming the actual transferred amount matches the computed `balance_to_unbond`. Either (a) revert if `transferable_balance` cannot cover the computed dissolve amount above a dust threshold, mirroring the Ajna fix of reverting when `fromPosition.lps > dust_threshold`, or (b) retain a residual claim record (e.g., re-credit the shortfall back into `sub_pools`/`unbonding_eras`, or track it in a "trapped balance" ledger similar to the mechanism already added in `pr_11018.prdoc` for a related trapped-balance bug) [10](#0-9)  so that any shortfall is never linked only to state that gets deleted in the same call.

### Proof of Concept
1. A pool member unbonds points at era `E`; `sub_pools.with_era[unbond_era]` accrues balance/points, and the member's `unbonding_eras[unbond_era]` is set accordingly [11](#0-10) .
2. Before the member calls `withdraw_unbonded`, the pool's bonded/delegate account's `transferable_balance` becomes smaller than what `sub_pools` accounting implies is owed to this member (e.g., due to an earlier member's withdrawal dusting the stash, exactly as the in-code comment describes, or via `DelegateStake`'s `delegator_balance` diverging from `agent_transferable_balance`) [4](#0-3) .
3. The member calls `withdraw_unbonded`. `withdraw_unlocked` unconditionally removes their `unbonding_eras` entry, `sub_pools` points are dissolved to zero, `balance_to_unbond` is computed then capped down by `.min(transferable_balance)` [12](#0-11) , and the reduced amount is paid out via `member_withdraw` [13](#0-12) .
4. If `member.total_points()` is now zero, `PoolMembers::<T>::remove(&member_account)` is executed [6](#0-5) , permanently erasing any trace of the unpaid difference; the member has no further call path to recover it.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L668-686)
```rust
	fn withdraw_unlocked(
		&mut self,
		active_era: EraIndex,
	) -> BoundedBTreeMap<EraIndex, BalanceOf<T>, T::MaxUnbonding> {
		// NOTE: if only drain-filter was stable..
		let mut removed_points =
			BoundedBTreeMap::<EraIndex, BalanceOf<T>, T::MaxUnbonding>::default();
		self.unbonding_eras.retain(|e, p| {
			if *e > active_era {
				true
			} else {
				removed_points
					.try_insert(*e, *p)
					.expect("source map is bounded, this is a subset, will be bounded; qed");
				false
			}
		});
		removed_points
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2293-2323)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2391-2399)
```rust
		#[pallet::call_index(5)]
		#[pallet::weight(
			T::WeightInfo::withdraw_unbonded_kill(*num_slashing_spans)
		)]
		pub fn withdraw_unbonded(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			num_slashing_spans: u32,
		) -> DispatchResultWithPostInfo {
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2437-2496)
```rust
			// NOTE: must do this after we have done the `ok_to_withdraw_unbonded_other_with` check.
			let withdrawn_points = member.withdraw_unlocked(active_era);
			ensure!(!withdrawn_points.is_empty(), Error::<T>::CannotWithdrawAny);

			// Before calculating the `balance_to_unbond`, we call withdraw unbonded to ensure the
			// `transferable_balance` is correct.
			let stash_killed = T::StakeAdapter::withdraw_unbonded(
				Pool::from(bonded_pool.bonded_account()),
				num_slashing_spans,
			)?;

			// defensive-only: the depositor puts enough funds into the stash so that it will only
			// be destroyed when they are leaving.
			ensure!(
				!stash_killed || caller == bonded_pool.roles.depositor,
				Error::<T>::Defensive(DefensiveError::BondedStashKilledPrematurely)
			);

			if stash_killed {
				// Maybe an extra consumer left on the pool account, if so, remove it.
				if frame_system::Pallet::<T>::consumers(&pool_account) == 1 {
					frame_system::Pallet::<T>::dec_consumers(&pool_account);
				}

				// Note: This is not pretty, but we have to do this because of a bug where old pool
				// accounts might have had an extra consumer increment. We know at this point no
				// other pallet should depend on pool account so safe to do this.
				// Refer to following issues:
				// - https://github.com/paritytech/polkadot-sdk/issues/4440
				// - https://github.com/paritytech/polkadot-sdk/issues/2037
			}

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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2500-2505)
```rust
			T::StakeAdapter::member_withdraw(
				Member::from(member_account.clone()),
				Pool::from(bonded_pool.bonded_account()),
				balance_to_unbond,
				num_slashing_spans,
			)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2514-2519)
```rust
			let post_info_weight = if member.total_points().is_zero() {
				// remove any `ClaimPermission` associated with the member.
				ClaimPermissions::<T>::remove(&member_account);

				// member being reaped.
				PoolMembers::<T>::remove(&member_account);
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L392-400)
```rust
	fn transferable_balance(
		pool_account: Pool<Self::AccountId>,
		member_account: Member<Self::AccountId>,
	) -> BalanceOf<T> {
		Delegation::agent_transferable_balance(pool_account.clone().into())
			// pool should always be an agent.
			.defensive_unwrap_or_default()
			.min(Delegation::delegator_balance(member_account.into()).unwrap_or_default())
	}
```

**File:** prdoc/stable2512-3/pr_11018.prdoc (L1-13)
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
```
