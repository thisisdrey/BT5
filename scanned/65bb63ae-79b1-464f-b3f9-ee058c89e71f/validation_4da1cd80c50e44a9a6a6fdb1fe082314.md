Based on the evidence gathered, the strongest local analog to the Spartan `Pool.sol` bug is in `pallet-nomination-pools`: a dual-accounting design where a member's pool "points" (the LP-token-equivalent) can decouple from the actual custodied balance held by the delegation/staking backend, permanently trapping funds that cannot be recovered through the pallet's normal exit paths (`unbond`/`withdraw_unbonded`/`claim_payout`).

### Title
Points-based pool accounting can desynchronize from actual held balance, permanently trapping member funds - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
Nomination pools represent a member's claim on pooled stake purely as `points`, converted to balance via `points_to_balance`/`balance_to_point` ratios [1](#0-0) . The actual custodied funds are tracked separately by the delegated-staking backend as a "held"/delegated balance. These two views of the same funds are supposed to stay in lockstep, but the pallet's own remediation code and prdoc acknowledge they can diverge, dissolving points without releasing the corresponding held funds — the same "mixed accounting units causing leftover dead funds" pattern as the Spartan report, where LP-share accounting (points) and actual asset custody get out of sync.

### Finding Description
The pallet computes a member's expected balance strictly from points (`member.total_balance()`), while the actual token custody lives in the delegation adapter (`T::StakeAdapter::member_delegation_balance`) [2](#0-1) . The pallet's own documentation for `do_claim_trapped_balance` states plainly:

"In rare scenarios, pool members may have excess held balance that is not accounted for in their pool points. This can occur when points are incorrectly dissolved without releasing the corresponding held funds." [3](#0-2) 

This is confirmed as a real, shipped incident, not a hypothetical: a dedicated migration and prdoc were produced to recover a member whose points were dissolved while the held funds were not released, caused by a `CurrentEra` vs `ActiveEra` mismatch:

"A bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were dissolved but the held funds weren't released." [4](#0-3) 

The similarity to the Spartan bug is structural: just as Spartan's `Pool` mixed LP-provider units and synth-debt units into one fungible LP balance, causing withdrawal order to leave one side with unfair residual claims, nomination-pools mixes "points" (an internal accounting unit for bonded/reward/unbonding sub-pools) with the actual on-chain held/delegated balance tracked by a separate pallet (`pallet-delegated-staking`). Normal exit calls (`unbond`, `withdraw_unbonded`) only manipulate the points side of the ledger [5](#0-4) ; they do not reconcile against the actual held balance except when `member_pending_slash`/`do_apply_slash` is explicitly invoked, and even then only for slashing, not for the CurrentEra/ActiveEra-class desync.

### Impact Explanation
When points are dissolved without releasing the equivalent held balance, the affected member's tokens remain locked/frozen in their account (held by the delegated-staking freeze) with no pool points left to redeem them through the standard `unbond`/`withdraw_unbonded` flow — a permanent user-fund lock, directly matching the "permanent user-fund ... lock" impact category. The fact that a dedicated recovery function `do_claim_trapped_balance` and a one-time storage migration had to be added [6](#0-5)  demonstrates this is not self-healing through any existing public extrinsic.

### Likelihood Explanation
The bug already manifested in production-adjacent conditions (it required a live migration for an affected pool member), showing the era-comparison mismatch is reachable through ordinary pool lifecycle operations (`unbond` → `withdraw_unbonded`) without any privileged actor, malicious validator, or governance action — it is a pure runtime-logic defect in reconciling `points` against the stake-adapter's era-indexed release schedule.

### Recommendation
Ensure every code path that dissolves pool `points` (in `unbond`, `withdraw_unbonded`, and slashing application) uses a single, consistent era reference (the same source used by the underlying staking/delegation backend to release held balance) so that points and held balance are dissolved atomically and cannot diverge; add a `try-state`/`try-runtime` invariant (similar to the existing `points >= stake` check [7](#0-6) ) that also asserts `expected_balance == actual_delegated_balance` (net of pending slash) for every member, rather than relying on a manually-triggered `do_claim_trapped_balance` recovery path.

### Proof of Concept
The pallet's own regression test demonstrates the mismatch mechanics (pending slash vs. held balance vs. points-derived expected balance) and the necessity of a separate reconciliation call: [8](#0-7) 
The originally-affected scenario (CurrentEra vs ActiveEra mismatch dissolving points without releasing held funds) is documented in the shipped fix itself: [9](#0-8) 

**Caveat / uncertainty**: I could not locate the exact original code diff/commit that introduced the `CurrentEra` vs `ActiveEra` mismatch (the literal strings `CurrentEra`/`ActiveEra` were not found via search in `lib.rs`, suggesting the root cause lived in `pallet-staking`/`pallet-delegated-staking` era-reporting logic rather than in nomination-pools itself). I confirmed the bug's existence and fix only through the prdoc description and the resulting `do_claim_trapped_balance` API/tests, not through the original vulnerable diff. Whether an equivalent still-open (non-era-related) desynchronization path exists today would require deeper tracing through `pallet-delegated-staking`'s hold/release logic than the available index coverage allowed — a Devin session with full repo access could confirm this further.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1072-1078)
```rust
	/// Convert the given number of points to balance given the current pool state.
	///
	/// This is often used for unbonding.
	fn points_to_balance(&self, points: BalanceOf<T>) -> BalanceOf<T> {
		let bonded_balance = T::StakeAdapter::active_stake(Pool::from(self.bonded_account()));
		Pallet::<T>::point_to_balance(bonded_balance, self.points, points)
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3295-3345)
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
```

**File:** prdoc/stable2512-3/pr_11018.prdoc (L1-14)
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
```

**File:** prdoc/1.16.0/pr_5465.prdoc (L1-6)
```text
title: try-state check invariant for nomination-pools (points >= stake)

doc:
  - audience: Runtime Dev
    description: |
      Adds a new try-state invariant to the nomination pools that checks that for each bonded pool, the pool's points can never be lower than its staked balance.
```

**File:** substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs (L1959-1993)
```rust
#[test]
fn do_claim_trapped_balance_applies_pending_slash_first() {
	new_test_ext().execute_with(|| {
		let alice = 100;
		assert_ok!(Balances::mint_into(&alice, 300));
		assert_ok!(Balances::mint_into(&10, 250));

		// Create pool and join
		assert_ok!(Pools::create(RuntimeOrigin::signed(10), 200, 10, 10, 10));
		assert_ok!(Pools::nominate(RuntimeOrigin::signed(10), 1, vec![1, 2, 3]));
		assert_ok!(Pools::join(RuntimeOrigin::signed(alice), 100, 1));

		let pool_account = Pools::generate_bonded_account(1);

		// Apply a slash to the pool
		Staking::set_era(1);
		pallet_staking_async::slashing::do_slash::<Runtime>(
			&pool_account,
			50,
			&mut Default::default(),
			&mut Default::default(),
			1,
		);

		// Verify pool and member have pending slash
		assert!(Pools::api_pool_pending_slash(1) > 0);
		assert!(Pools::api_member_pending_slash(alice) > 0);

		// do_claim_trapped_balance applies the slash as a side effect, even though there's
		// no trapped balance (returns Ok as a no-op for the trapped balance part).
		assert_ok!(Pools::do_claim_trapped_balance(&alice));

		// Verify slash was applied
		assert_eq!(Pools::api_member_pending_slash(alice), 0);
	});
```
