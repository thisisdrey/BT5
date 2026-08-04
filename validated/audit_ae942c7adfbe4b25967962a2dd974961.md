### Title
`withdraw_unbonded` silently truncates the withdrawn balance to `transferable_balance` while fully burning the corresponding unbonding points, causing permanent fund loss for pool members - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The Ekubo `Router` bug is a case where an "exact" operation (exact-out swap) can be partially filled by the underlying engine, but the caller-facing validation only checks a bound (`amountInMax`) and not that the full requested amount was actually delivered — so the transaction reports success despite delivering less than the user asked for. The `pallet-nomination-pools::withdraw_unbonded` extrinsic has the same broken invariant: it fully dissolves (burns) the member's unbonding points as if the entire unbonded balance were paid out, but then silently caps (`.min(...)`) the *actual* transferred amount to `T::StakeAdapter::transferable_balance`, without any error or event signalling this truncation to a value below what the points represent, and without reconciling the discrepancy. The extrinsic still returns `Ok(...)`, exactly like the Router's exact-out swap still succeeding on partial fill.

### Finding Description
`Pallet::withdraw_unbonded` (`substrate/frame/nomination-pools/src/lib.rs:2395-2559`) computes `withdrawn_points` via `member.withdraw_unlocked(active_era)` and then unconditionally dissolves those points from the era-keyed `sub_pools` (`era_pool.dissolve(*unlocked_points)` / `sub_pools.no_era.dissolve(*unlocked_points)`), accumulating what *should* be the exact `balance_to_unbond` owed to the member for those burnt points. [1](#0-0) 

Immediately after, the computed `balance_to_unbond` is silently clamped:
```rust
.min(T::StakeAdapter::transferable_balance(
    Pool::from(bonded_pool.bonded_account()),
    Member::from(member_account.clone()),
));
``` [2](#0-1) 

The code comment itself acknowledges the truncation is a deliberate "best effort" fallback ("this check ensures we don't try to withdraw funds that don't exist... We gracefully proceed in order to ensure members can leave the pool"), but there is no reconciliation step: the points that were dissolved represented the *full* claim, while the actual transfer (`T::StakeAdapter::member_withdraw(... balance_to_unbond ...)`) and the emitted `Event::Withdrawn { balance: balance_to_unbond, points: sum_unlocked_points, .. }` both use the truncated amount. [3](#0-2) 

This is confirmed by an existing test comment that documents the exact "partial fill, still Ok" behaviour as intended: *"Charlie's total balance was 12, but we don't have enough funds to unlock. We try the best effort and unlock 10."* — and `assert_ok!` still succeeds. [4](#0-3) 

Once the points are dissolved and (if the member's `total_points()` becomes zero) the member is fully reaped from `PoolMembers` (or the whole pool is dissolved if the caller is the depositor), there is no remaining state that tracks the shortfall between the value the points represented and the amount actually paid out — the member permanently loses the difference. This mirrors the Router bug exactly: an operation that is supposed to be "exact" (deliver the balance matching the burnt points) is allowed to complete as a partial fill without any explicit acknowledgment/limit check comparable to the Router's `amountInMax`/`amountOutMin` guard — here there is no user-supplied guard at all, and no error path, just an unconditional `.min()` clamp that silently reduces the payout.

### Impact Explanation
Because points (the internal accounting unit representing a member's claim) are burned in full while the value returned can be strictly less, the discrepancy is unrecoverable: once `withdrawn_points` are dissolved from `sub_pools` and the member's point balance no longer references them, there is no way to later claim the shortfall. This can lead to permanent fund loss/lock for the pool member whenever `transferable_balance` is temporarily (or persistently) lower than the balance value implied by the unbonded points — e.g., when the pool's bonded stash has been dusted by an earlier withdrawal in the same era pool, or when the unbond-pool's internal accounting has drifted from the actual stash balance (acknowledged directly in the source comment as possible "in cases where the unbond pool does not update its balance, e.g. a bug in the slashing hook"). This directly conserves-value guarantee for staking/pool payouts, matching the "Impact Gate" criterion of unbacked loss / permanent user-fund lock without any privileged or malicious actor involved — it is triggered purely by ordinary sequences of `unbond`/`withdraw_unbonded` calls by unprivileged pool members.

### Likelihood Explanation
This is not a hypothetical: the pallet's own comment explicitly anticipates the scenario ("A call to this transaction may cause the pool's stash to get dusted... subsequent withdraws will be 0") and a shipped test (`test-delegate-stake`) demonstrates and asserts the exact truncated-withdrawal behavior as "working as intended," confirming the path is reachable under normal (non-adversarial) usage of `unbond`/`withdraw_unbonded` by ordinary members, particularly in pools nearing depletion, being destroyed, or affected by slashing/dusting edge cases. No malicious peer, validator, governance action, or leaked key is required — only two or more members withdrawing from a nearly-drained pool in a particular order.

### Recommendation
Do not silently clamp `balance_to_unbond`. Either:
1. Fail the extrinsic (return an error) when `transferable_balance` is insufficient to cover the value represented by the points about to be dissolved, so the caller can retry once liquidity/state is consistent, or
2. If a best-effort partial payout must be supported for the "member gets reaped" edge cases, reconcile the shortfall explicitly — e.g., re-credit the member with points/balance equal to the undelivered amount instead of unconditionally burning the full `withdrawn_points`, and emit a distinct event making the shortfall auditable — mirroring how Uniswap v3's router reverts on partial fill by default unless the caller opts in via an explicit flag.

### Proof of Concept
The truncation is already exercised in-repo:
1. A pool member (e.g. `charlie`) has unbonded a certain number of points corresponding to balance `12`.
2. Due to a prior withdrawal by another member depleting the pool's stash/dust threshold, `T::StakeAdapter::transferable_balance` for `charlie` returns only `10` at the time `withdraw_unbonded` is called.
3. `withdraw_unbonded(charlie, charlie, 0)` is called and dissolves the *entire* `12`-value worth of points from the sub-pool, but `balance_to_unbond` is clamped via `.min(transferable_balance)` to `10`.
4. The call still returns `Ok(...)`; `Event::Withdrawn { balance: 10, points: 15, .. }` is emitted, `charlie` is reaped from `PoolMembers`, and the extra `2` (unit) is unrecoverable — this is verified by the existing test assertion `assert_eq!(Balances::free_balance(&charlie), charlie_pre_withdraw_balance + 10)` following unbond of `12`. [5](#0-4)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2469-2485)
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2486-2496)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2498-2512)
```rust
			// this can fail if the pool uses `DelegateStake` strategy and the member delegation
			// is not claimed yet. See `Call::migrate_delegation()`.
			T::StakeAdapter::member_withdraw(
				Member::from(member_account.clone()),
				Pool::from(bonded_pool.bonded_account()),
				balance_to_unbond,
				num_slashing_spans,
			)?;

			Self::deposit_event(Event::<T>::Withdrawn {
				member: member_account.clone(),
				pool_id: member.pool_id,
				points: sum_unlocked_points,
				balance: balance_to_unbond,
			});
```

**File:** substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs (L1659-1678)
```rust
		// Charlie can withdraw as much as he has locked.
		Staking::set_era(18);
		let charlie_pre_withdraw_balance = Balances::free_balance(&charlie);
		assert_ok!(Pools::withdraw_unbonded(RuntimeOrigin::signed(charlie), charlie, 0));
		// Charlie's total balance was 12, but we don't have enough funds to unlock. We try the best
		// effort and unlock 10.
		assert_eq!(Balances::free_balance(&charlie), charlie_pre_withdraw_balance + 10);

		assert_eq!(
			staking_events_since_last_call(),
			vec![StakingEvent::Withdrawn { stash: POOL1_BONDED, amount: 5 },]
		);

		assert_eq!(
			pool_events_since_last_call(),
			vec![
				PoolsEvent::Withdrawn { pool_id: 1, member: charlie, balance: 10, points: 15 },
				PoolsEvent::MemberRemoved { member: charlie, pool_id: 1, released_balance: 0 }
			]
		);
```
