## Analysis

The external report's core broken invariant: an "entitled" withdrawal amount is computed from an aggregate accounting model (`totalAssets`/`maxWithdraw`), but the amount actually paid out is limited by a *different*, narrower real-balance check — and the accounting state is allowed to advance/destroy the user's claim *before* it is confirmed that the real transfer can happen for the full amount. In pUSDe this caused a revert-lock; the same pattern exists in `pallet-nomination-pools`, except there it causes a **silent, permanent loss of the differential amount** rather than merely a revert.

### Title
Nomination pool member unbonding points are irreversibly burned before the actual transferable balance is verified, causing silent, permanent loss of unbonded funds - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::withdraw_unbonded` computes `balance_to_unbond` from the unbonding sub-pool's points-to-balance ratio, **mutating and persisting the sub-pool's points/balance state as part of that computation**, and only afterwards clamps the result with `.min(T::StakeAdapter::transferable_balance(...))` before actually paying the member via `T::StakeAdapter::member_withdraw`. If the real transferable/liquid balance held by the staking backend for that pool/member is lower than what the points-ratio computation implies, the shortfall is discarded silently: the member's points and the sub-pool's points/balance have already been "dissolved" and removed from storage, but the member only receives the smaller, clamped amount. [1](#0-0) 

### Finding Description
In `withdraw_unbonded`:
1. `member.withdraw_unlocked(active_era)` permanently removes the member's unbonding points for the matured eras.
2. The fold loop calls `era_pool.dissolve(*unlocked_points)` (or `sub_pools.no_era.dissolve(...)`), which **mutates the local `sub_pools`** by removing both the points and the corresponding balance from the unbonding sub-pool — this is an unconditional state mutation based purely on the points ratio.
3. Only after this destructive computation is `balance_to_unbond` clamped: `.min(T::StakeAdapter::transferable_balance(...))`.
4. `sub_pools` (already mutated in step 2, irrespective of the clamp in step 3) is then persisted via `SubPoolsStorage::<T>::insert(member.pool_id, sub_pools)`, and only the clamped `balance_to_unbond` is actually transferred to the member via `T::StakeAdapter::member_withdraw`.

The code's own comment acknowledges the scenario ("A call to this transaction may cause the pool's stash to get dusted... this check is also defensive in cases where the unbond pool does not update its balance (e.g. a bug in the slashing hook.)"), but treats it purely as a "best effort" guard against a revert — it does not address that the member's claim (points) is already destroyed for the full, un-clamped amount before the payout is confirmed. There is no re-crediting of the un-paid delta back to the member or to the sub-pool.

This is structurally identical to the pUSDe bug: an aggregate/points-based "entitlement" is treated as ground truth and used to mutate accounting state, while the actual redeemable amount is bounded by a separate, narrower balance source (`transferable_balance`, sourced from the underlying `StakeAdapter`/staking backend) that can diverge from the points-based expectation (e.g., due to lazy/partial slashing application timing, `DelegateStake` adapter desync between agent ledger `unclaimed_withdrawals` and the sub-pool's bookkeeping, or dust removal of the pool stash mid-flow).

### Impact Explanation
Any divergence between the sub-pool's points-derived `balance_to_unbond` and the pool account's actual `transferable_balance` at the moment of withdrawal results in **permanent, silent loss of the shortfall** for the withdrawing member — the points are already burned from `SubPoolsStorage`, so there is no path to reclaim the difference later. This falls squarely under "permanent user-fund or bridge-state lock" / "settlement state advancing before execution succeeds atomically," which are explicitly in-scope impact categories.

### Likelihood Explanation
This does not require any privileged actor, malicious validator, or governance abuse — it depends only on normal pool operation combined with adapter/ledger state divergence explicitly acknowledged in the pallet's own comments (dusting of the pool stash mid-sequence, or slashing-hook bugs affecting unbond pool balances vs. the `StakeAdapter`'s view). Because the mutation of `sub_pools` happens unconditionally before the clamp, any legitimate scenario that triggers this divergence turns into fund loss rather than a safe no-op or revert.

### Recommendation
Compute the clamp (`min` with `T::StakeAdapter::transferable_balance`) **before** mutating/dissolving the sub-pool's points and balance, or perform the dissolve based on the already-clamped amount so that the sub-pool's points/balance only reflect what is actually paid out. Alternatively, re-credit any un-paid delta back into the sub-pool (or the member's active points) so the member retains a claim on it instead of losing it.

### Proof of Concept
Conceptually mirrors the pUSDe PoC:
1. Member unbonds points corresponding to `X` tokens according to the sub-pool's points-to-balance ratio.
2. Due to a discrepancy between the sub-pool bookkeeping and `T::StakeAdapter::transferable_balance` (e.g., partial dusting of the bonded/agent account, or a slashing-hook bug that under-updates the unbond pool balance as the code comment itself anticipates), the actual transferable balance is `Y < X`.
3. `withdraw_unbonded` dissolves the sub-pool for the full points value (removing the accounting for `X`), persists `SubPoolsStorage`, but only transfers `Y` to the member via `member_withdraw`.
4. The member has lost `X - Y` permanently: their points are gone, the sub-pool's tracked balance for those points is gone, and no mechanism exists to reclaim the difference. [2](#0-1)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2438-2505)
```rust
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

			// this can fail if the pool uses `DelegateStake` strategy and the member delegation
			// is not claimed yet. See `Call::migrate_delegation()`.
			T::StakeAdapter::member_withdraw(
				Member::from(member_account.clone()),
				Pool::from(bonded_pool.bonded_account()),
				balance_to_unbond,
				num_slashing_spans,
			)?;
```
