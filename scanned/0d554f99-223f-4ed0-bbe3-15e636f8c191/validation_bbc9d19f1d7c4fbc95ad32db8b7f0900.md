## Title
`withdraw_unbonded` burns a pool member's unbonding points while silently paying out less than owed when the pool's transferable balance is depleted, with no revert on shortfall - ([File: substrate/frame/nomination-pools/src/lib.rs])

## Summary
The nomination-pools pallet's `withdraw_unbonded` extrinsic computes the amount to release from a member's unlocked unbonding points, then clamps that amount to the pool's currently available `transferable_balance` via `.min(...)`, without checking whether the clamp actually reduced the payout. The points are burned from the member's unbonding record unconditionally regardless of how much balance is actually transferred, mirroring the H-04 pattern where a controller silently accepts under-liquidity withdrawals instead of reverting.

## Finding Description
In `Pallet::withdraw_unbonded` [1](#0-0) , `member.withdraw_unlocked(active_era)` permanently removes the member's unbonding points from their record, and this happens before the payout amount is determined.

The actual payout, `balance_to_unbond`, is computed by folding over the withdrawn points and dissolving the corresponding sub-pools (which also permanently mutates/removes `sub_pools.with_era`/`no_era` entries), and is then clamped: [2](#0-1) 

```rust
let balance_to_unbond = withdrawn_points
    .iter()
    .fold(...)
    // ...
    .min(T::StakeAdapter::transferable_balance(
        Pool::from(bonded_pool.bonded_account()),
        Member::from(member_account.clone()),
    ));
```

The comment explicitly documents this as a "graceful" best-effort behavior rather than an error path: it exists "in cases where the unbond pool does not update its balance ... we gracefully proceed in order to ensure members can leave the pool." There is no `ensure!` that `balance_to_unbond` equals the value implied by the burned points, nor any check that `balance_to_unbond > 0`.

`transferable_balance` for the (default) `TransferStake` adapter is simply the pool account's reducible free balance [3](#0-2) , which is a shared resource across all pool members. Any member (or the depositor, or an attacker who has bonded/unbonded into the pool) can race to drain this shared, unlocked balance via their own `withdraw_unbonded` call before a victim's transaction lands in the same block. Because `member_withdraw` is called afterward with the already-clamped `balance_to_unbond` [4](#0-3) , a `balance_to_unbond` of 0 still results in a successful no-op transfer, an emitted `Event::Withdrawn` with `balance: 0`, and the member being fully or partially removed/reaped with their points already burned [5](#0-4) .

This is the direct structural analog of the H-04 report: the "controller" (here, the pools pallet's withdraw path) never raises an error when there's insufficient liquidity in the underlying account; instead it silently proceeds, burning the user's claim (shares/points) while paying out less than intended.

## Impact Explanation
A victim's legitimate `withdraw_unbonded` extrinsic can execute successfully but transfer 0 or a reduced amount of tokens, while their unbonding points/claims are permanently and irreversibly burned from `SubPoolsStorage` and `PoolMembers`. This is a direct, unbacked loss of user funds within the pools pallet, matching the "permanent user-fund lock/loss" and "duplicate settlement inconsistency" impact classes for this scope. The victim has no recourse — the extrinsic returns `Ok`, not an error, so no retry/refund path exists.

## Likelihood Explanation
This requires no privileged access, malicious validator/collator, or off-chain infrastructure — only an unprivileged account that is (or becomes) a pool member with unbonded points eligible for withdrawal, racing transaction ordering within a single block (a standard, permitted MEV/ordering primitive on Substrate chains, same class as the original sandwich attack). The condition is most easily triggered on pools using the legacy `TransferStake` adapter (still supported, `#[deprecated]` but not removed) since `transferable_balance` there is literally the shared pool account's free balance [3](#0-2) , and is also possible to a lesser extent under `DelegateStake` since `transferable_balance` there is bounded by `agent_transferable_balance`, a value shared/consumed across all delegators of the pool [6](#0-5) .

## Recommendation
- After computing `balance_to_unbond`, `ensure!` that it is not less than the amount implied by the sum of dissolved points (or, at minimum, revert / avoid burning points/sub-pool entries if `transferable_balance` cannot cover the requested amount), instead of silently clamping via `.min(...)`.
- Alternatively, refuse to remove the member's unbonding record / dissolve sub-pools for the portion of points that cannot be paid out, so a member's unclaimed points remain intact and can be withdrawn later when liquidity is available, rather than being destroyed for nothing.
- At minimum, add a check that reverts the whole extrinsic (`Error::<T>::CannotWithdrawAny` or a new dedicated error) when the final `balance_to_unbond` is `0` due to clamping, rather than emitting a `Withdrawn { balance: 0 }` event with points already burned.

## Proof of Concept
1. Two members, A (attacker) and B (victim), are in the same pool using `TransferStake` (or `DelegateStake`) with unbonded points ready to withdraw at the same era.
2. B submits `withdraw_unbonded(B, ..)` intending to withdraw their unlocked balance.
3. A observes B's pending transaction and submits their own `withdraw_unbonded(A, ..)` with higher priority/tip in the same block, which succeeds and drains the pool account's `transferable_balance` (free/reducible balance) to near zero.
4. B's `withdraw_unbonded(B, ..)` then executes: `member.withdraw_unlocked` removes B's points, `sub_pools` dissolve reduces/removes B's era pool entries, but `balance_to_unbond.min(transferable_balance)` clamps the payout to whatever tiny amount (or zero) remains.
5. `T::StakeAdapter::member_withdraw` transfers the clamped (possibly zero) amount to B; the call returns `Ok`, `Event::Withdrawn { member: B, balance: <near-zero>, points: <full amount> }` is emitted, and B may even be reaped from `PoolMembers` — despite having received nothing close to their entitled balance.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2437-2439)
```rust
			// NOTE: must do this after we have done the `ok_to_withdraw_unbonded_other_with` check.
			let withdrawn_points = member.withdraw_unlocked(active_era);
			ensure!(!withdrawn_points.is_empty(), Error::<T>::CannotWithdrawAny);
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2498-2505)
```rust
			// this can fail if the pool uses `DelegateStake` strategy and the member delegation
			// is not claimed yet. See `Call::migrate_delegation()`.
			T::StakeAdapter::member_withdraw(
				Member::from(member_account.clone()),
				Pool::from(bonded_pool.bonded_account()),
				balance_to_unbond,
				num_slashing_spans,
			)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2507-2519)
```rust
			Self::deposit_event(Event::<T>::Withdrawn {
				member: member_account.clone(),
				pool_id: member.pool_id,
				points: sum_unlocked_points,
				balance: balance_to_unbond,
			});

			let post_info_weight = if member.total_points().is_zero() {
				// remove any `ClaimPermission` associated with the member.
				ClaimPermissions::<T>::remove(&member_account);

				// member being reaped.
				PoolMembers::<T>::remove(&member_account);
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L270-276)
```rust
	fn transferable_balance(
		pool_account: Pool<Self::AccountId>,
		_: Member<Self::AccountId>,
	) -> BalanceOf<T> {
		// free/liquid balance of the pool account.
		T::Currency::reducible_balance(&pool_account.get(), Expendable, Polite)
	}
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
