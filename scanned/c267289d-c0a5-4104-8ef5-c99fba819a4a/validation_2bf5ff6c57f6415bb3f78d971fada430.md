### Title
Pool-member funds can become permanently trapped when the era used to record an unbonding chunk (`current_era()`) diverges from the era used elsewhere to reconcile the member's sub-pool balance, mirroring the "closed SR during a stale-index dispute" fund-loss pattern - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools`'s `unbond` records an unbonding chunk keyed by an era computed from `T::StakeAdapter::current_era()`, while the later reconciliation logic in `withdraw_unbonded`/sub-pool bookkeeping must match that same era key to release the correspondingly-held balance. The codebase itself documents (via `prdoc/stable2512-2/pr_10986.prdoc` and `prdoc/stable2512-3/pr_11018.prdoc`) that a `CurrentEra` vs `ActiveEra` mismatch caused a pool member's points to be dissolved from pool storage while the actual held balance was never released — i.e., the member's economic position was "closed" (points removed) but the value-transfer step that should accompany that closure did not execute, exactly like the DittoETH pattern where a short record is closed while a dispute proposal referencing it is still mid-flight and collateral gets silently absorbed with no way to reclaim it.

### Finding Description
`unbond` computes the unbonding-chunk key as: [1](#0-0) 
using `T::StakeAdapter::current_era()`, and inserts the dissolved balance into `SubPoolsStorage` under that era key, then records `unbonding_eras` in the `PoolMember` struct via `member.try_unbond(..., unbond_era)`.

Later, `withdraw_unbonded` computes `active_era` again from `T::StakeAdapter::current_era()`: [2](#0-1) 
and calls `member.withdraw_unlocked(active_era)` to decide which `unbonding_eras` entries are due, then looks up the matching `sub_pools.with_era[era]` bucket to dissolve points and compute `balance_to_unbond`: [3](#0-2) 

The repo's own change history confirms these two "era" concepts (`CurrentEra`, used for election-cycle bookkeeping, vs `ActiveEra`, the era actually reflected in staking ledgers/rewards) were inconsistently used across pools/staking call sites, and that this specific divergence caused member points to be dissolved from `PoolMembers`/`SubPoolsStorage` (i.e., the record is "closed") without releasing the corresponding held balance in `pallet-balances` via `T::StakeAdapter::member_withdraw`: [4](#0-3) [5](#0-4) 

The remediation shipped is a private helper, `do_claim_trapped_balance`, which re-derives the expected balance from points and releases any excess held balance — but it is exercised only through a one-time, hardcoded migration for the single already-known affected account, not exposed as a general-purpose, permissionless extrinsic that any future affected member could call on demand: [6](#0-5) 

This is the structural analog of the DittoETH bug: a stateful accounting record (there, a Short Record referenced by stale index in a redemption dispute; here, a `PoolMember`'s unbonding chunk referenced by an era key) can be closed/dissolved by one code path while a second, dependent code path — responsible for actually moving the corresponding value — relies on a different, potentially-diverged piece of state (era value) to find and release that value. When the two pieces of state disagree, the accounting entry is deleted but the value transfer never happens, and the funds become unreachable through the pallet's normal call surface.

### Impact Explanation
If the `CurrentEra`/`ActiveEra` divergence recurs for any other pool member (e.g., a fast-unbonding validator/nominator transition, an era rollback edge case, or any other path that reads `current_era()` at a different rotation boundary than the one used when the unbonding chunk was created), that member's `unbonding_eras` entry will be consumed (`withdraw_unlocked` removes it from `PoolMember`) but the underlying `SubPools`/`UnbondPool` bucket will not match, causing `balance_to_unbond` to be computed incorrectly (potentially zero, per the defensive `.min(...)` clamp) while the member's points are already gone. The result is a permanent, unrecoverable loss of the member's staked funds inside the pool's aggregate held balance, with no general on-chain path to reclaim it (only a bespoke governance-driven migration for a manually-identified case).

### Likelihood Explanation
The bug class is proven to have occurred at least once in production/testing (hence the dedicated migration), and the underlying root-cause pattern — two independently-computed era values used to key the same accounting relationship — remains present in the current `unbond`/`withdraw_unbonded` implementation. Any future divergence between `current_era()` calls made at different block/session boundaries (a scenario the PR titles explicitly call out as a general hazard, not a one-off fluke) can reproduce the same trapped-balance condition for a new member, and there is no permissionless self-service recovery function guarding against recurrence.

### Recommendation
- Ensure a single, canonical era source (the active era, not current era) is used consistently across `unbond`, `withdraw_unbonded`, and all `SubPools`/`UnbondPool` era-keyed storage operations, and add an invariant check (e.g., in `try-runtime` / `integrity_test`) asserting that `sum(unbonding_eras balances)` always equals the sum of the corresponding `SubPools` bucket balances for every member.
- Expose `do_claim_trapped_balance` as a permissionless, generally-callable extrinsic (not just an internal helper invoked by a one-time migration) so any member who becomes affected by a future occurrence of this divergence can recover trapped funds without requiring a bespoke governance migration.
- Add regression tests that deliberately desynchronize `CurrentEra` and `ActiveEra` across an `unbond` → era-rotation → `withdraw_unbonded` sequence to confirm funds are never dissolved-but-unreleased.

### Proof of Concept
A concrete PoC requires reproducing a `CurrentEra`/`ActiveEra` divergence at the exact block where `unbond` computes `unbond_era` vs. where `withdraw_unbonded`/era-rotation reconciles `SubPoolsStorage`; the repository's own existing tests demonstrate the shape of the underlying mechanics that make this possible: [7](#0-6) 
combined with the documented real-world occurrence and fix trail: [5](#0-4) 
Because the exact triggering conditions of the historical occurrence are not fully described in the indexed files, a full step-by-step exploit transaction sequence cannot be reconstructed with certainty from this repository snapshot alone — this is flagged as the main uncertainty in this analysis, and would require deeper investigation of `pallet-staking-async`'s era-rotation (`Rotator`) logic in a live Devin session to pin down precisely which call paths can currently still diverge.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2290-2295)
```rust
			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2408-2416)
```rust
			let mut member =
				PoolMembers::<T>::get(&member_account).ok_or(Error::<T>::PoolMemberNotFound)?;
			let active_era = T::StakeAdapter::current_era();

			let bonded_pool = BondedPool::<T>::get(member.pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?;
			let mut sub_pools =
				SubPoolsStorage::<T>::get(member.pool_id).ok_or(Error::<T>::SubPoolsNotFound)?;

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2469-2496)
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3295-3356)
```rust
	/// Claim trapped balance for a pool member.
	///
	/// In rare scenarios, pool members may have excess held balance that is not accounted
	/// for in their pool points. This can occur when points are incorrectly dissolved
	/// without releasing the corresponding held funds.
	///
	/// If the pool has any pending slash, it will be applied to the member first before
	/// claiming the trapped balance.
	///
	/// Safe to call multiple times or for non-existent members — returns `Ok(())` as a
	/// no-op when there is nothing to do.
	pub fn do_claim_trapped_balance(member_account: &T::AccountId) -> DispatchResult {
		ensure!(
			T::StakeAdapter::strategy_type() == adapter::StakeStrategyType::Delegate,
			Error::<T>::NotSupported
		);

		// Apply any pending slash first. Ignore NothingToSlash and PoolMemberNotFound
		// (member existence is validated below).
		match Self::do_apply_slash(member_account, None, false) {
			Ok(_) => {},
			Err(e)
				if e == Error::<T>::NothingToSlash.into() ||
					e == Error::<T>::PoolMemberNotFound.into() => {},
			Err(_) => {
				return Err(Error::<T>::Defensive(DefensiveError::SlashNotApplied).into());
			},
		};

		let member = match PoolMembers::<T>::get(member_account) {
			Some(m) => m,
			None => return Ok(()),
		};

		let expected_balance = member.total_balance();
		let actual_balance =
			T::StakeAdapter::member_delegation_balance(Member::from(member_account.clone()))
				.unwrap_or_default();

		let trapped_amount = actual_balance.saturating_sub(expected_balance);

		if trapped_amount.is_zero() {
			return Ok(());
		}

		T::StakeAdapter::member_withdraw(
			Member::from(member_account.clone()),
			Pool::from(Self::generate_bonded_account(member.pool_id)),
			trapped_amount,
			0,
		)?;

		log!(
			info,
			"Claimed trapped balance for member {:?}, pool {:?}, amount {:?}",
			member_account,
			member.pool_id,
			trapped_amount
		);

		Ok(())
	}
```

**File:** prdoc/stable2512-2/pr_10986.prdoc (L1-9)
```text
title: '[Pool] Use active era for withdrawals'
doc:
- audience: Runtime Dev
  description: Standardising using active era in pools and staking. Current Era should
    only be used for election logic
crates:
- name: pallet-nomination-pools
  bump: patch
- name: pallet-staking-async
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

**File:** substrate/frame/nomination-pools/src/tests.rs (L3954-4011)
```rust
	#[test]
	fn withdraw_unbonded_works_against_slashed_with_era_sub_pools() {
		ExtBuilder::default()
			.add_members(vec![(40, 40), (550, 550)])
			.build_and_execute(|| {
				let _ = balances_events_since_last_call();
				// Given
				// current bond is 600, we slash it all to 300.
				StakingMock::slash_by(1, 300);
				assert_eq!(StakingMock::total_stake(&default_bonded_account()), Ok(300));

				assert_ok!(fully_unbond_permissioned(40));
				assert_ok!(fully_unbond_permissioned(550));

				assert_eq!(
					SubPoolsStorage::<Runtime>::get(1).unwrap().with_era,
					unbonding_pools_with_era! { 3 => UnbondPool { points: 550 / 2 + 40 / 2, balance: 550 / 2 + 40 / 2
					}}
				);

				assert_eq!(
					pool_events_since_last_call(),
					vec![
						Event::Created { depositor: 10, pool_id: 1 },
						Event::Bonded { member: 10, pool_id: 1, bonded: 10, joined: true },
						Event::MetadataUpdated { pool_id: 1, caller: 900 },
						Event::Bonded { member: 40, pool_id: 1, bonded: 40, joined: true },
						Event::Bonded { member: 550, pool_id: 1, bonded: 550, joined: true },
						Event::PoolSlashed { pool_id: 1, balance: 300 },
						Event::Unbonded { member: 40, pool_id: 1, balance: 20, points: 20, era: 3 },
						Event::Unbonded {
							member: 550,
							pool_id: 1,
							balance: 275,
							points: 275,
							era: 3,
						}
					]
				);

				CurrentEra::set(StakingMock::bonding_duration());

				// When
				assert_ok!(Pools::withdraw_unbonded(RuntimeOrigin::signed(40), 40, 0));

				// Then
				assert_eq!(
					pool_events_since_last_call(),
					vec![
						Event::Withdrawn { member: 40, pool_id: 1, balance: 20, points: 20 },
						Event::MemberRemoved { pool_id: 1, member: 40, released_balance: 0 }
					]
				);

				assert_eq!(
					SubPoolsStorage::<Runtime>::get(1).unwrap().with_era,
					unbonding_pools_with_era! { 3 => UnbondPool { points: 550 / 2, balance: 550 / 2 }}
				);
```
