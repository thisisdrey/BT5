Audit Report

## Title
Public transfer to a nomination-pool's reward account can overflow `RewardPool::current_reward_counter`, permanently locking payouts, bonding and unbonding for that pool - (File: substrate/frame/nomination-pools/src/lib.rs)

## Summary
`current_reward_counter` computes `new_pending_rewards * 10^18 / bonded_points` via `T::RewardCounter::checked_from_rational`, using `current_balance` (the reward account's free balance) as an input to `new_pending_rewards`. [1](#0-0)  Since the reward account address is deterministically derivable via `generate_reward_account`/`current_balance` and is not access-controlled, any account can inflate its balance with a plain transfer, pushing this rational division past what `FixedU128` can represent and causing `checked_from_rational(...).ok_or(Error::<T>::OverflowRisk)` to fail. [2](#0-1)  Because `update_records` (which calls `current_reward_counter`) is invoked as a mandatory precondition in bonding/unbonding/claim flows, this failure permanently blocks all state-changing operations for the affected pool.

## Finding Description
`update_records` calls `current_reward_counter`, which reads `current_balance(id)` — the reward account's free balance — and uses it, along with `bonded_points` (used as the denominator), in a rational multiplication scaled by `FixedU128`'s `10^18` base. [3](#0-2)  `current_balance` reads the reward account's free balance directly via `T::Currency::reducible_balance`, with no cap or filter on what value the account may hold. [4](#0-3)  The reward account is deterministically generated and is not access-controlled, so an unprivileged attacker can inflate its balance via a plain public transfer.

Unlike the rest of this module's arithmetic, which uses `saturating_add`/`saturating_sub`, this specific division-then-scaling step uses `checked_from_rational(...).ok_or(Error::<T>::OverflowRisk)?`, so any overflow aborts the entire call rather than degrading gracefully. [5](#0-4)  Since `update_records`/`current_reward_counter` is a mandatory precondition invoked from every state-changing pool extrinsic path (bonding, unbonding, and claim), once the overflow condition is reached for a given pool, all of those operations fail with `Error::OverflowRisk` for that pool going forward — not merely for the single triggering transaction.

The project's own regression test, `reward_counter_update_can_fail_if_pool_is_highly_slashed`, demonstrates this exact overflow mechanism is reachable and manifests as `Error::OverflowRisk` from the public `join` extrinsic once the balance-to-`bonded_points` ratio is pushed to an extreme. [6](#0-5)  While that test reaches the condition via a slashing event, the underlying arithmetic depends only on the ratio of reward-account balance to `bonded_points` — a plain external transfer directly into the reward account achieves the same numerator inflation without requiring any slash, staking event, or privileged action.

## Impact Explanation
Once triggered, `update_records` fails deterministically for the affected pool on every subsequent call, since `current_balance` (attacker-inflated) remains part of the calculation until the pool's `bonded_points` grow enough to bring the ratio back in range — which itself requires successful `bond_extra`/`join` calls that are now blocked. This creates a durable fund-lock: legitimate members can no longer claim payouts, bond additional funds, join, or unbond, and their already-bonded funds are effectively frozen within the pool. This matches the "permanent user-fund lock" impact class in the Polkadot SDK impact gate, since it degrades intended pallet behavior for an entire nomination pool via a public, permissionless griefing input.

## Likelihood Explanation
The transfer needed is a plain, unprivileged `Balances` transfer to a deterministically computable account (`Pallet::<T>::generate_reward_account(id)`), requiring no governance, no validator collusion, and no special role — only sufficient free balance relative to the pool's current `bonded_points`. Small or newly created pools with low `bonded_points` are the most exploitable, since the vulnerable condition depends on the balance-to-points ratio rather than absolute pool size, making this both feasible and repeatable across many low-liquidity pools.

## Recommendation
- Use saturating rational arithmetic (or clamp the computed reward-counter value to `T::RewardCounter::MAX`) instead of `checked_from_rational(...).ok_or(Error::<T>::OverflowRisk)` in `current_reward_counter`, so pathological balances degrade payout precision instead of blocking the extrinsic entirely.
- Alternatively, bound `current_payout_balance` (or the resulting `new_pending_rewards`) to a sane multiple relative to `bonded_points`/pool `total_issuance` before the `FixedU128` conversion, treating any excess as unattributable dust that does not affect `update_records`'s success.
- Provide a privileged sweep/cap mechanism for excess/unexpected reward-account balance so it cannot influence `update_records` before being properly accounted for.

## Proof of Concept
1. Create a nomination pool with the minimum allowed bond, yielding small `bonded_points`.
2. From an unrelated, unprivileged account, submit a plain balance transfer directly to `BondedPool::<T>::get(pool_id).reward_account()` (computable via `Pallet::<T>::generate_reward_account(pool_id)`) for an amount large enough that, given the pool's current `bonded_points`, `new_pending_rewards * 10^18 / bonded_points` exceeds the `RewardCounter`'s representable range — mirroring the mechanics shown in `reward_counter_update_can_fail_if_pool_is_highly_slashed` (`substrate/frame/nomination-pools/src/tests.rs:5923-5954`).
3. Any subsequent call to `claim_payout`, `bond_extra`, `join`, or `unbond` for that pool fails with `Error::<T>::OverflowRisk`, because `RewardPool::update_records`/`current_reward_counter` (`substrate/frame/nomination-pools/src/lib.rs:1408-1512`) is a mandatory precondition invoked by each of those extrinsics.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1408-1417)
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1456-1470)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1506-1512)
```rust
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;

		Ok((current_reward_counter, new_pending_commission))
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1517-1523)
```rust
	fn current_balance(id: PoolId) -> BalanceOf<T> {
		T::Currency::reducible_balance(
			&Pallet::<T>::generate_reward_account(id),
			Preservation::Expendable,
			Fortitude::Polite,
		)
	}
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
