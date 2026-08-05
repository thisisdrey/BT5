## Title
Reentrancy-style CEI violation in `do_reward_payout` allows duplicate reward claims before storage is persisted - (File: `substrate/frame/nomination-pools/src/lib.rs`)

## Summary
The external report describes a classic checks-effects-interactions (CEI) violation: a state-mutating value transfer happens before the invariant-protecting storage write (`lastPriceTimestamp`), letting a callback-capable recipient re-enter and repeat the payout before the state that would block it is committed. The same broken ordering exists in `pallet-nomination-pools::do_reward_payout`, where the reward-accounting mutations (`member.last_recorded_reward_counter`, `reward_pool.register_claimed_reward`) are only applied to **local, in-memory copies** of `PoolMember`/`RewardPool`, and the `T::Currency::transfer` to the claiming member happens *before* those mutated copies are written back to storage by the caller.

## Finding Description
`do_reward_payout` computes `pending_rewards`, updates the in-memory `member` and `reward_pool` structs, and only then calls `T::Currency::transfer`: [1](#0-0) 

The mutated `member`/`reward_pool` are **not** written to storage inside this function — persistence only happens afterward, in the caller, via `put_member_with_pools`: [2](#0-1) 

This ordering is repeated in every public entrypoint that calls `do_reward_payout`: `claim_payout`/`do_claim_payout`, `bond_extra`, and `unbond` all call `do_reward_payout(...)` first and only call `Self::put_member_with_pools(...)` (or equivalent storage inserts) afterward: [3](#0-2) [4](#0-3) [5](#0-4) 

Because `PoolMembers::<T>` and `RewardPools::<T>` on-chain storage still hold the **pre-payout** values at the moment `T::Currency::transfer` executes, any code path that can be re-entered from within that transfer will observe stale state: `member.pending_rewards(current_reward_counter)` will recompute the same non-zero `pending_rewards`, and the `ensure!(!member.active_points().is_zero(), ...)` guard will still pass, allowing a second (or repeated) payout for the same accrued reward before the first payout's bookkeeping is ever committed.

This is structurally identical to the reported bug: an accounting update is computed but deferred past an external value transfer, so a reentrant call sees the pre-update state and repeats a payout.

## Impact Explanation
If reached, this allows an attacker to drain a pool's `RewardPool` reward account by repeatedly claiming the same accrued reward multiple times within a single reentrant call chain, before the pallet's own bookkeeping (`last_recorded_reward_counter`, `total_rewards_claimed`) is ever updated in storage. This directly violates the "reward payouts must conserve value and settle exactly once" invariant called out in the impact gate.

## Likelihood Explanation
`T::Currency` in `pallet-nomination-pools` is a generic `Mutate`/`Currency` bound, not hardcoded to `pallet-balances`. On chains where the pallet is configured against `pallet-balances` directly (Polkadot/Kusama today), `transfer` performs no external call and is not reentrant, so the path is currently latent rather than actively exploitable in the shipped runtimes. However, the vulnerable ordering exists unconditionally in the pallet code itself, independent of the currency backend, and would become immediately exploitable the moment nomination-pools is wired to any `Currency`/`fungible` implementation that invokes external logic on transfer (e.g., an asset/fungibles adapter with transfer hooks). This mirrors exactly the conditional nature of the original report ("if the settlement token contains a callback on transfers").

## Recommendation
Apply strict checks-effects-interactions ordering in `do_reward_payout`: persist the updated `member` and `reward_pool` to storage (or have the caller do so) **before** invoking `T::Currency::transfer`, not after. Concretely, move the `PoolMembers::<T>::insert` / `RewardPools::insert` writes ahead of the transfer call inside `do_reward_payout`, or restructure the function so it returns the amount to transfer without performing the transfer itself, letting the caller write state first and transfer second.

## Proof of Concept
1. Configure (or imagine a future configuration of) `pallet-nomination-pools::Config::Currency` with a fungible implementation whose `transfer` invokes a callback into the receiving account (e.g., a fungibles adapter bridging to a contract-controlled asset, analogous to an ERC777-style hook).
2. Member `A` accrues pending rewards in a pool.
3. `A`'s account is configured/controlled such that receiving the `T::Currency::transfer` triggers a callback that re-invokes `claim_payout` (or `bond_extra`/`unbond`) for the same `member_account` before `put_member_with_pools` has executed for the first call.
4. Because `PoolMembers::<T>` and `RewardPools::<T>` in storage are unchanged at this point (`do_reward_payout`'s local mutations of `member.last_recorded_reward_counter` and `reward_pool.register_claimed_reward` were never persisted), `Self::get_member_with_pools(&member_account)` in the nested call recomputes the same `pending_rewards`, `ensure!(!member.active_points().is_zero())` still holds, and a second `T::Currency::transfer` fires for the same reward.
5. Repeat while the reward pool account still has funds, draining it beyond the true entitlement of `A`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2275-2288)
```rust
			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3457-3471)
```rust
	fn put_member_with_pools(
		member_account: &T::AccountId,
		member: PoolMember<T>,
		bonded_pool: BondedPool<T>,
		reward_pool: RewardPool<T>,
	) {
		// The pool id of a member cannot change in any case, so we use it to make sure
		// `member_account` is the right one.
		debug_assert_eq!(PoolMembers::<T>::get(member_account).unwrap().pool_id, member.pool_id);
		debug_assert_eq!(member.pool_id, bonded_pool.id);

		bonded_pool.put();
		RewardPools::insert(member.pool_id, reward_pool);
		PoolMembers::<T>::insert(member_account, member);
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3547-3563)
```rust
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}

		// IFF the reward is non-zero alter the member and reward pool info.
		member.last_recorded_reward_counter = current_reward_counter;
		reward_pool.register_claimed_reward(pending_rewards);

		T::Currency::transfer(
			&bonded_pool.reward_account(),
			member_account,
			pending_rewards,
			// defensive: the depositor has put existential deposit into the pool and it stays
			// untouched, reward account shall not die.
			Preservation::Preserve,
		)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3668-3683)
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3763-3770)
```rust
		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?;

		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);
		Ok(())
	}
```
