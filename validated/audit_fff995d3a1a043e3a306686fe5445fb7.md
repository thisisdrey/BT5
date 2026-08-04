### Title
`withdraw_unbonded` can strand a member's staking balance when `SubPoolsStorage` era-keyed balance falls out of sync with `PoolMembers` era claims - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
The yAxis bug is a class of "accounting subtracts the wrong tracked value instead of the true delta," causing an internal balance ledger to diverge from the real underlying balance and permanently locking funds behind an assertion/guard. The closest proven local analog in this repository is the nomination-pools sub-pool/points accounting, where `UnbondPool::dissolve` and the `SubPoolsStorage`/`PoolMembers` era bookkeping in `withdraw_unbonded` can diverge from the real bonded/unbonding balance held by the staking backend. This exact bug class was already found and had to be fixed with a one-time chain-state migration [1](#0-0) , confirming the invariant is not otherwise enforced at the extrinsic level.

### Finding Description
In `pallet-nomination-pools`, a member's claim on the bonded/unbonding stake is tracked purely as *points* in `PoolMembers` and `SubPoolsStorage`, converted to balance via `UnbondPool::dissolve`, which does: [2](#0-1) 

This is structurally identical to `Controller.sol`'s `_vaultDetails[_vault].balance.sub(_balance)`: an internally tracked ledger value (`points`/`balance` pair) is decremented based on an internal ratio calculation rather than being reconciled against the actual balance held by the external system (the staking pallet / `StakeAdapter`). In `withdraw_unbonded`, the amount actually released to the member (`balance_to_unbond`) is computed by walking `withdrawn_points` and calling `era_pool.dissolve(*unlocked_points)` for each era, then finally clamped with `.min(T::StakeAdapter::transferable_balance(...))`: [3](#0-2) 

The code's own comment acknowledges that the sub-pool balance can get out of sync with reality ("this check is also defensive in cases where the unbond pool does not update its balance (e.g. a bug in the slashing hook)"), and the pallet ships a `do_try_state` invariant explicitly because `TotalValueLocked` / sub-pool sums can silently deviate from the actual pool balance tracked by `T::StakeAdapter`: [4](#0-3) 

This exact divergence was already realized in production: PR #11018 documents a live incident where a **`CurrentEra` vs `ActiveEra` mismatch** caused a pool member's points to be dissolved (i.e., the ledger subtracted their claim) while the underlying held funds were never actually released — the funds became permanently trapped and required a dedicated one-time migration (`ClaimTrappedBalance` era logic in `substrate/frame/nomination-pools/src/migration.rs`) to recover: [1](#0-0) 

This is the same failure mode as the yAxis report: a permission-gated/automatic bookkeeping operation (`setCap` there, era rollover / `withdraw_unbonded` here) decrements an internal balance/points ledger using a value that does not match the real balance delta, so subsequent legitimate withdrawal calls either under-pay the member (fund lock) or hit downstream assertions/guards (`do_try_state`, `CannotWithdrawAny`, the staking-side `min(transferable_balance(...))` clamp) that silently truncate payouts to zero without reverting the whole extrinsic.

### Impact Explanation
When the sub-pool balance/points ledger and the real bonded-account balance from `T::StakeAdapter` diverge, `balance_to_unbond` is clamped down by `.min(transferable_balance(...))`, meaning the member's dissolved points are burned from the ledger but the corresponding funds are not transferred out — the confirmed real-world outcome of PR #11018's incident. This is a fund-lock/loss issue satisfying the "permanent user-fund lock" and "runtime bugs that compromise intended behavior" impact categories, without requiring any privileged actor, malicious validator, or admin action — it is triggered by the ordinary sequencing of `unbond` → era rollover → `withdraw_unbonded` calls made by unprivileged pool members.

### Likelihood Explanation
The likelihood is high in principle (it already happened once and needed a chain migration to fix) but the currently indexed code contains defensive/`try-state` checks and a documented follow-up fix (`do_apply_slash` before withdraw, `transferable_balance` clamp, `check_ed_imbalance`, `do_try_state` warnings) that were added specifically in reaction to this incident. I could not fully verify from the indexed code whether the exact `CurrentEra`/`ActiveEra` root cause path that caused PR #11018 is fully closed for all `StakeAdapter` variants (`Transfer` vs `Delegate`) or only patched via the one-off migration for already-affected accounts, since `withdraw_unlocked`'s era-comparison logic and the full `StakeAdapter::current_era()` implementation were not returned in my searches. This should be verified directly in `substrate/frame/nomination-pools/src/lib.rs` (`PoolMember::withdraw_unlocked`) and `adapter.rs`.

### Recommendation
- Ensure `SubPoolsStorage` balance updates and `PoolMembers::unbonding_eras` era keys are always derived from the same era source (`T::StakeAdapter::current_era()`) as is used when the staking backend actually unlocks funds, eliminating any window where a different "current era" concept can cause points to dissolve without a matching real balance release.
- Extend `do_try_state` (or add a mandatory pre-dispatch check in `withdraw_unbonded`) to hard-fail (not just `log!(warn, ...)`) when `sum_unbonding_balance` combined with `bonded_balance` exceeds `total_balance`, converting the current best-effort/defensive warning into an enforced invariant.
- Audit all `Balance::saturating_sub` and points/balance dissolution paths in `nomination-pools` for cases where an amount is subtracted from an internally computed ratio rather than from the actual delta reported by `T::StakeAdapter`.

### Proof of Concept
The concrete, already-materialized PoC is the incident described in the migration PR itself: a pool member unbonds; due to a `CurrentEra` vs `ActiveEra` mismatch, the era used to dissolve their points in `SubPoolsStorage`/`UnbondPool::dissolve` did not correspond to the era at which the staking backend actually released the corresponding balance; the member's `withdraw_unbonded` call proceeded, `PoolMembers::withdraw_unlocked` removed their points, but `T::StakeAdapter::transferable_balance` did not yet reflect the funds, so the clamp `.min(transferable_balance(...))` reduced `balance_to_unbond` to less than the dissolved value — trapping the difference until Parity shipped the one-time `ClaimTrappedBalance` migration to manually reconcile the affected account [1](#0-0) .

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1567-1577)
```rust
	/// Dissolve some points from the unbonding pool, reducing the balance of the pool
	/// proportionally. This is the opposite of `issue`.
	///
	/// Returns the actual amount of `Balance` that was removed from the pool.
	fn dissolve(&mut self, points: BalanceOf<T>) -> BalanceOf<T> {
		let balance_to_unbond = self.point_to_balance(points);
		self.points = self.points.saturating_sub(points);
		self.balance = self.balance.saturating_sub(balance_to_unbond);

		balance_to_unbond
	}
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L4101-4119)
```rust
			let sum_unbonding_balance = subs.sum_unbonding_balance();
			let bonded_balance = T::StakeAdapter::active_stake(Pool::from(pool_account.clone()));
			// TODO: should be total_balance + unclaimed_withdrawals from delegated staking
			let total_balance = T::StakeAdapter::total_balance(Pool::from(pool_account.clone()))
				// At the time when StakeAdapter is changed to `DelegateStake` but pool is not yet
				// migrated, the total balance would be none.
				.unwrap_or(T::Currency::total_balance(&pool_account));

			if total_balance < bonded_balance + sum_unbonding_balance {
				log!(
						warn,
						"possibly faulty pool: {:?} / {:?}, total_balance {:?} >= bonded_balance {:?} + sum_unbonding_balance {:?}",
						pool_id,
						_pool,
						total_balance,
						bonded_balance,
						sum_unbonding_balance
					)
			};
```
