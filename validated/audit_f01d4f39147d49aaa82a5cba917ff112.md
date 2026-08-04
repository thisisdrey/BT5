Based on the code gathered, the strongest local analog to the memory/storage checkpoint bug is `do_reward_payout` in `substrate/frame/nomination-pools/src/lib.rs`.

### Title
`do_reward_payout` mutates member/reward-pool state only in-memory, relying on callers to persist it - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`Pallet::do_reward_payout` computes pending rewards and updates `member.last_recorded_reward_counter` and `reward_pool.register_claimed_reward(..)` purely on the `&mut PoolMember<T>` / `&mut RewardPool<T>` structs passed in, and transfers the actual currency payout to the member — but it never writes the updated `member`/`reward_pool` back to `PoolMembers`/`RewardPools` storage itself. The function's own doc comment even states: *"does not persist any of the mutable inputs to storage."* [1](#0-0)  Persistence is only performed afterwards by the caller via `Self::put_member_with_pools(...)` [2](#0-1) . This mirrors exactly the `ERC20ConvictionScore._writeCheckpoint` pattern: the accounting update happens on a memory-only copy, and a real side effect (token/asset movement) is unconditionally executed based on that copy, while the durable state write is a separate, easily-detached step.

### Finding Description
`do_reward_payout` is the single core routine used by `do_bond_extra` and `do_claim_payout` to calculate and pay a member's pending pool rewards: [3](#0-2) 

It:
1. Computes `current_reward_counter` from `reward_pool.current_reward_counter(...)`.
2. Computes `pending_rewards` from `member.pending_rewards(current_reward_counter)`.
3. Mutates the **in-memory** `member.last_recorded_reward_counter` and calls `reward_pool.register_claimed_reward(pending_rewards)` (also in-memory only).
4. Executes a real `T::Currency::transfer` of `pending_rewards` from the reward account to the member — an unconditional, irreversible-looking side effect performed *before* any storage write of the updated counters.

The actual commit to storage (`PoolMembers`, `RewardPools`, `BondedPool`) happens only in the caller, via `put_member_with_pools`, called once at the very end of `do_bond_extra` [4](#0-3)  and `do_claim_payout` [5](#0-4) . This is structurally identical to the Solidity bug: the "checkpoint" (reward counters) is only advanced in a memory struct, and any code path that calls the payout logic twice on structs fetched from the same pre-write storage state, or any path that fails to reach the final `put_member_with_pools`/`insert`, will pay out currency without the corresponding counter update ever landing in storage.

### Impact Explanation
If the updated `member`/`reward_pool` values are not persisted (e.g., a future or feature-gated code path calls `do_reward_payout` without following through with `put_member_with_pools`, or nested calls operate on two independently-fetched copies of the same `PoolMembers`/`RewardPools` entry within one transaction), the member's `last_recorded_reward_counter` and the pool's `total_commission_pending`/claimed counters revert to their stale on-chain values while the currency transfer has already executed. This directly breaks the "settle exactly once" invariant for pooled staking rewards: a member could re-claim the same reward repeatedly (or an inconsistency between `RewardPools` totals and actual reward-account balance could accumulate), draining the pool's reward account beyond what it should pay, i.e., unbacked payout from the reward pot.

### Likelihood Explanation
Currently, both production call sites (`do_bond_extra`, `do_claim_payout`) do correctly call `put_member_with_pools` right after `do_reward_payout`, so under the code as currently wired there is no live double-payout path — the "guard" is that both existing callers happen to persist. However, this guard is not enforced by the type system or by `do_reward_payout` itself; nothing prevents a new call site, or a refactor, from fetching `member`/`reward_pool`, calling `do_reward_payout`, and returning early (e.g. on an error in an unrelated line before reaching `put_member_with_pools`) — the transfer already fired but the counter update is discarded when the extrinsic's storage transaction is not the vehicle actually committing the state (transfer already happened outside of that discarded write). This makes the function inherently fragile: correctness depends entirely on caller discipline rather than an enforced invariant, exactly the same root cause as the original H-04 finding.

### Recommendation
Refactor `do_reward_payout` so that persisting the updated `member` and `reward_pool` (and any dependent `bonded_pool` state) is not left to caller discipline: either have it directly write to `PoolMembers::<T>::insert` / `RewardPools::<T>::insert` before performing the `T::Currency::transfer`, or restructure the API so it is impossible to call without an atomic accompanying storage write (e.g., make it return the structs and force `put_member_with_pools` via a single combined function that cannot be split apart). At minimum, add a debug/defensive assertion or `#[must_use]`-style enforcement ensuring every call to `do_reward_payout` is immediately followed by persistence in the same code block, and audit all current and future call sites for this invariant.

### Proof of Concept
Conceptual PoC (illustrating the broken invariant, since currently no call site actually omits the persistence step):
1. Fetch `(member, bonded_pool, reward_pool)` via `get_member_with_pools`.
2. Call `Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)` — this transfers `pending_rewards` currency to the member immediately.
3. Before calling `put_member_with_pools`, take an early return/error on an unrelated check (or, in a modified/future code path, call `do_reward_payout` a second time using a second independently-fetched copy of the same storage entries within the same block/transaction).
4. Because `PoolMembers`/`RewardPools` in on-chain storage were never updated with the new `last_recorded_reward_counter` / `register_claimed_reward`, a subsequent call to `do_claim_payout` in the same block recomputes `pending_rewards` from the *stale* reward counter and pays out the same rewards again, resulting in the reward account being drained beyond its backing balance.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L3455-3471)
```rust
	/// Persist the member with their associated bonded and reward pool into storage, consuming
	/// all of them.
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3524-3571)
```rust
	/// If the member has some rewards, transfer a payout from the reward pool to the member.
	// Emits events and potentially modifies pool state if any arithmetic saturates, but does
	// not persist any of the mutable inputs to storage.
	fn do_reward_payout(
		member_account: &T::AccountId,
		member: &mut PoolMember<T>,
		bonded_pool: &mut BondedPool<T>,
		reward_pool: &mut RewardPool<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		debug_assert_eq!(member.pool_id, bonded_pool.id);
		debug_assert_eq!(&mut PoolMembers::<T>::get(member_account).unwrap(), member);

		// a member who has no skin in the game anymore cannot claim any rewards.
		ensure!(!member.active_points().is_zero(), Error::<T>::FullyUnbonding);

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

		Self::deposit_event(Event::<T>::PaidOut {
			member: member_account.clone(),
			pool_id: member.pool_id,
			payout: pending_rewards,
		});
		Ok(pending_rewards)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3766-3769)
```rust
		Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?;

		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);
		Ok(())
```
