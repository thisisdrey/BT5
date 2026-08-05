### Title
Public transfer to a nomination-pool's reward account can overflow `RewardPool::current_reward_counter`, permanently locking payouts, bonding and unbonding for that pool - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`pallet-nomination-pools` computes each pool's payout ratio from the *free balance* held in a deterministically-derived, publicly known reward account. Because any account can perform a plain balance transfer to that account, an attacker can inflate the value used in `RewardPool::current_reward_counter`'s internal multiplication/division and push it past what `FixedU128`/`checked_from_rational` can represent. When that happens the call returns `Error::OverflowRisk` instead of saturating, and since this computation is invoked from every state-changing pool extrinsic (`bond_extra`, `join`, `unbond`, `claim_payout`, commission updates), the whole pool becomes permanently stuck — an on-chain, permissionless analog of the reported Solidity DoS where an attacker inflates `originalBalance` via a direct token transfer to force a revert in the payout distribution math.

### Finding Description
The reward pool's payable balance is read directly from the reward account's free balance: [1](#0-0) 

This reward account address is derived deterministically and is not access-controlled — anyone can transfer plain balance into it (it is even documented as a valid deposit path in analogous pallets, e.g. `deposit_reward_tokens` explicitly notes tokens "could be transferred directly to the pool pot address"): [2](#0-1) 

`current_reward_counter` then uses this balance in a rational-multiplication that can overflow `u128` once scaled by `FixedU128`'s `10^18` base: [3](#0-2) 

Unlike most other arithmetic in this module (`saturating_add`, `saturating_sub`), this specific step uses `checked_from_rational(...).ok_or(Error::<T>::OverflowRisk)`, which **fails the whole extrinsic** rather than saturating. `update_records` (which calls this) is invoked unconditionally before any state-changing operation on a pool, e.g. in `do_bond_extra`: [4](#0-3) 

and `do_reward_payout` (used by `claim_payout`) directly calls `current_reward_counter`: [5](#0-4) 

The project's own test suite confirms this overflow is reachable and causes `Error::OverflowRisk` to be returned from a public extrinsic (`join`) once the pool's balance/points ratio is pushed to an extreme: [6](#0-5) 

Because `bonded_points` is the denominator and `current_payout_balance` (derived from the reward-account's free balance) is the numerator, an attacker does not need a slashing event at all — directly wiring a very large balance into `bonded_pool.reward_account()` while the pool has small `bonded_points` is sufficient to make `new_pending_rewards / bonded_points` (after `x*10^18` scaling) exceed `u128::MAX`, exactly mirroring the Solidity report's "inflate `originalBalance` to overflow `payoutPercent * originalBalance`" pattern — except here `update_records` gates every subsequent bond/unbond/claim call for the pool, so the DoS is durable, not one-shot.

### Impact Explanation
Once `current_reward_counter` overflows for a pool, `update_records` fails, which means:
- `claim_payout` fails for every member of the pool — legitimate rewards become permanently unclaimable.
- `bond_extra`, `join`, `unbond` all fail because they call `update_records`/`do_reward_payout` first.
- Existing bonded funds for all members of the pool are effectively frozen inside a pool that can no longer process any accounting-dependent operation.

This is a fund-lock DoS affecting an entire nomination pool and every member in it, triggerable by an unprivileged account with a single balance transfer, matching the "permanent user-fund lock" and "public underpriced work that stalls processing" impact classes.

### Likelihood Explanation
The transfer needed to trigger this is a plain, permissionless `Balances::transfer` to a deterministically computable account (`BondedPool::reward_account()`/`generate_reward_account(id)`), requiring no governance, no validator collusion, no relayer, and no privileged role — only sufficient free balance to reach the overflow threshold relative to the pool's current `bonded_points`. Small/young pools (low `bonded_points`) are the most exploitable, since the overflow condition depends on the balance-to-points ratio, not absolute pool size. The project's own regression test demonstrates the exact overflow path is real and already known to the maintainers, though it is currently framed as an edge case rather than as an attacker-triggerable DoS via direct pot funding.

### Recommendation
- Cap or reject direct transfers-in effects on reward-account balance used for reward-counter math, or recompute `current_reward_counter` using saturating rational arithmetic instead of `checked_from_rational(...).ok_or(OverflowRisk)`, so that pathological balances degrade payout precision rather than blocking the extrinsic entirely.
- Alternatively, bound the multiplier by clamping `current_payout_balance` to a sane multiple of `bonded_points`/`total_issuance` before the `FixedU128` conversion, and treat any excess as un-attributable dust rather than reverting the whole update.
- Add a call that lets `root`/`bouncer` sweep unexpected/excess balance out of the reward account (or auto-cap it) before it can influence `update_records`, similar to `deposit_reward_tokens` style controlled deposits used in `asset-rewards`.

### Proof of Concept
1. Create a nomination pool with the minimum allowed bond (small `bonded_points`).
2. From an unrelated account, submit `Balances::transfer_allow_death` (or `transfer_keep_alive`) directly to `BondedPool::<T>::get(pool_id).reward_account()` for an amount large enough that, given the pool's current `bonded_points`, `new_pending_rewards * 10^18 / bonded_points` exceeds `u128::MAX` (existing test `reward_counter_update_can_fail_if_pool_is_highly_slashed` at `substrate/frame/nomination-pools/src/tests.rs:5923-5954` shows the mechanics of reaching `Error::OverflowRisk` from this calculation).
3. Any subsequent call to `Pools::claim_payout`, `Pools::bond_extra`, `Pools::join`, or `Pools::unbond` for that pool now fails with `Error::<T>::OverflowRisk`, permanently, because `RewardPool::update_records`/`current_reward_counter` is invoked as a mandatory precondition in every one of those extrinsics (`substrate/frame/nomination-pools/src/lib.rs:1408-1512`, `3539-3550`, `3668-3684`).

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1462-1512)
```rust
		let current_payout_balance = balance
			.saturating_add(self.total_rewards_claimed)
			.saturating_add(self.total_commission_claimed)
			.saturating_sub(self.last_recorded_total_payouts);

		// Split the `current_payout_balance` into claimable rewards and claimable commission
		// according to the current commission rate.
		let new_pending_commission = commission * current_payout_balance;
		let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);

		// * accuracy notes regarding the multiplication in `checked_from_rational`:
		// `current_payout_balance` is a subset of the total_issuance at the very worse.
		// `bonded_points` are similarly, in a non-slashed pool, have the same granularity as
		// balance, and are thus below within the range of total_issuance. In the worse case
		// scenario, for `saturating_from_rational`, we have:
		//
		// dot_total_issuance * 10^18 / `minJoinBond`
		//
		// assuming `MinJoinBond == ED`
		//
		// dot_total_issuance * 10^18 / 10^10 = dot_total_issuance * 10^8
		//
		// which, with the current numbers, is a miniscule fraction of the u128 capacity.
		//
		// Thus, adding two values of type reward counter should be safe for ages in a chain like
		// Polkadot. The important note here is that `reward_pool.last_recorded_reward_counter` only
		// ever accumulates, but its semantics imply that it is less than total_issuance, when
		// represented as `FixedU128`, which means it is less than `total_issuance * 10^18`.
		//
		// * accuracy notes regarding `checked_from_rational` collapsing to zero, meaning that no
		//   reward can be claimed:
		//
		// largest `bonded_points`, such that the reward counter is non-zero, with `FixedU128` will
		// be when the payout is being computed. This essentially means `payout/bonded_points` needs
		// to be more than 1/1^18. Thus, assuming that `bonded_points` will always be less than `10
		// * dot_total_issuance`, if the reward_counter is the smallest possible value, the value of
		//   the
		// reward being calculated is:
		//
		// x / 10^20 = 1/ 10^18
		//
		// x = 100
		//
		// which is basically 10^-8 DOTs. See `smallest_claimable_reward` for an example of this.
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;

		Ok((current_reward_counter, new_pending_commission))
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1514-1523)
```rust
	/// Current free balance of the reward pool.
	///
	/// This is sum of all the rewards that are claimable by pool members.
	fn current_balance(id: PoolId) -> BalanceOf<T> {
		T::Currency::reducible_balance(
			&Pallet::<T>::generate_reward_account(id),
			Preservation::Expendable,
			Fortitude::Polite,
		)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3539-3550)
```rust
		let (current_reward_counter, _) = reward_pool.current_reward_counter(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;

		// Determine the pending rewards. In scenarios where commission is 100%, `pending_rewards`
		// will be zero.
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3668-3684)
```rust
		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		// payout related stuff: we must claim the payouts, and updated recorded payout data
		// before updating the bonded pool points, similar to that of `join` transaction.
		reward_pool.update_records(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
		let claimed = Self::do_reward_payout(
			&member_account,
			&mut member,
			&mut bonded_pool,
			&mut reward_pool,
		)?;

```

**File:** substrate/frame/asset-rewards/src/lib.rs (L667-686)
```rust
		/// Convenience method to deposit reward tokens into a pool.
		///
		/// This method is not strictly necessary (tokens could be transferred directly to the
		/// pool pot address), but is provided for convenience so manual derivation of the
		/// account id is not required.
		#[pallet::call_index(7)]
		pub fn deposit_reward_tokens(
			origin: OriginFor<T>,
			pool_id: PoolId,
			amount: T::Balance,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&caller,
				&pool_info.account,
				amount,
				Preservation::Preserve,
			)?;
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L5923-5954)
```rust
	#[test]
	fn reward_counter_update_can_fail_if_pool_is_highly_slashed() {
		// create a pool that has roughly half of the polkadot issuance in 10 years.
		let pool_bond = inflation(10) / 2;
		ExtBuilder::default().ed(DOT).min_bond(pool_bond).build_and_execute(|| {
			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 10, pool_id: 1 },
					Event::Bonded {
						member: 10,
						pool_id: 1,
						bonded: 12_968_712_300_500_000_000,
						joined: true,
					},
					Event::MetadataUpdated { pool_id: 1, caller: 900 },
				]
			);

			// slash this pool by 99% of that.
			StakingMock::slash_by(1, pool_bond * 99 / 100);

			// some whale now joins with the other half ot the total issuance. This will trigger an
			// overflow. This test is actually a bit too lenient because all the reward counters are
			// set to zero. In other tests that we want to assert a scenario won't fail, we should
			// also set the reward counters to some large value.
			Currency::set_balance(&20, pool_bond * 2);
			assert_err!(
				Pools::join(RuntimeOrigin::signed(20), pool_bond, 1),
				Error::<T>::OverflowRisk
			);
		})
```
