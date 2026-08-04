### Title
Any nomination-pool member can grief the shared bonded stash's `MaxUnlockingChunks` to lock other members' funds for a full bonding duration - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`pallet-nomination-pools` funnels every member's `unbond` into a **single shared staking ledger** (the pool's bonded account). `pallet-staking`'s `StakingLedger` caps the number of concurrent unbonding entries at `T::MaxUnlockingChunks`, returning `Error::NoMoreChunks` once the bound is hit [1](#0-0) . Because this bound is enforced on the pool's *shared* stash, not per pool-member, any single unprivileged member can fill up all available chunks with small unbonds, causing all other members' `Pools::unbond` calls to revert until the oldest chunk ages out (a full `BondingDuration`). This is a direct structural analog of the RageTrade senior-vault issue: a shared, capacity-limited resource (there: utilization ratio; here: unlocking-chunk slots) that gates every user's ability to exit, and that any single user's activity can push into the locked/reverting regime.

### Finding Description
`Pools::unbond` calls `T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)`, which resolves to `pallet_staking::Pallet::unbond` operating on the pool's one shared bonded account [2](#0-1) . The staking pallet stores unbonding requests as a `BoundedVec<UnlockChunk<Balance>, T::MaxUnlockingChunks>` per ledger; when the current era's chunk already exists it is merged, but new eras add new chunks, and once the vector is full the operation fails with `Error::<T>::NoMoreChunks` rather than succeeding partially [1](#0-0) . This test explicitly demonstrates the failure mode against a nomination-pool bonded account with `MaxUnlockingChunks == 1`: a second unbond from a different member reverts with `NoMoreChunks` and only clears after waiting the full `BondingDuration` [3](#0-2) .

Because *all* pool members' unbonds land on the same underlying stash/ledger, an attacker who is any ordinary pool member (no privilege required) can:
1. Call `unbond` with a dust amount every era, for `MaxUnlockingChunks` consecutive eras, filling every unlocking slot on the shared ledger.
2. Any other member who then tries `Pools::unbond` gets `pallet_staking::Error::NoMoreChunks` surfaced through `nomination_pools::Error` — exactly the `beforeWithdraw`-style revert seen in the RageTrade report, except here it is trivially triggerable by an unprivileged actor rather than being a byproduct of organic borrow demand.
3. Victims must wait for the oldest attacker-created chunk to clear via `withdraw_unbonded`/`pool_withdraw_unbonded` after a full `BondingDuration`, before their own unbond can proceed — an attacker can perpetually re-fill the chunks each era to keep the lock in place indefinitely at negligible cost (dust bonds), similar to how RageTrade's utilization cap can indefinitely gate withdrawals.

This bypasses no explicit "cap monitoring" safeguard because none exists at the pool layer: `ok_to_unbond_with` checks membership/points/min-bond conditions but does not check or reserve unlocking-chunk capacity before calling into staking, nor does it rate-limit or attribute chunk usage per member [4](#0-3) .

### Impact Explanation
This does not steal funds directly, but it can **lock other users' unbonding out of the pool for an indefinite/extended period**, which maps to the required impact class of "permanent user-fund or bridge-state lock." A griefer with minimal capital (dust-sized repeated unbonds) can deny withdrawal liquidity to potentially large numbers of legitimate stakers in a nomination pool, degrading the pool's usability and potentially forcing users into unbonding delays well beyond the intended `BondingDuration`, especially if repeated every era.

### Likelihood Explanation
Likelihood is high: `unbond` is a public, permissionless, low-cost dispatchable available to any pool member; `MaxUnlockingChunks` is a fixed, typically small runtime constant (e.g. `32` in many configs, smaller in test runtimes), and the only cost to the attacker is transaction fees plus locking their own dust stake. No governance, admin, validator, or malicious-relayer assumption is required — it is purely a normal user exploiting an inherent shared-resource design gap.

### Recommendation
- Track and cap per-member (or fairly allocate) unlocking-chunk usage within `pallet-nomination-pools` rather than relying solely on the raw staking-ledger bound shared across all members.
- Alternatively, have `ok_to_unbond_with` pre-check remaining ledger unlocking-chunk capacity and either merge/queue member unbonds pool-side (batching multiple member unbonds into a single era-chunk on the underlying ledger, as already partially done for same-era requests) so that per-member unbond calls cannot individually consume a scarce global slot.
- Consider a minimum unbonding size or per-account rate limiting on `unbond` to make dust-based chunk exhaustion economically unattractive.

### Proof of Concept
1. Deploy a nomination pool with a `Runtime` where `MaxUnlockingChunks` is small (e.g. `1`, as configured in the referenced e2e test).
2. Have attacker (member A) call `Pools::unbond(A, A, 1)` in era 0 — this creates one unlocking chunk on the shared bonded account.
3. Advance to era 1 (chunk for era 0 is not the current era, so a new unbond call creates a second chunk request).
4. Have victim (member B) call `Pools::unbond(B, B, 1)` — this fails with `pallet_staking::Error::<Runtime>::NoMoreChunks`, exactly as shown in the existing test `substrate/frame/election-provider-multi-phase/test-staking-e2e/src/lib.rs:356-362` [5](#0-4) .
5. Member B is now locked out of unbonding until the full `BondingDuration` has elapsed, even though B's own request had nothing to do with A's dust unbond — demonstrating a griefing-induced fund lock via a shared, capacity-limited resource, directly analogous to the RageTrade utilization-cap withdrawal lock.

### Citations

**File:** substrate/frame/staking/src/ledger.rs (L277-296)
```rust
#[cfg(test)]
use {
	crate::UnlockChunk,
	codec::{Decode, Encode, MaxEncodedLen},
	scale_info::TypeInfo,
};

// This structs makes it easy to write tests to compare staking ledgers fetched from storage. This
// is required because the controller field is not stored in storage and it is private.
#[cfg(test)]
#[derive(frame_support::DebugNoBound, Clone, Encode, Decode, TypeInfo, MaxEncodedLen)]
pub struct StakingLedgerInspect<T: Config> {
	pub stash: T::AccountId,
	#[codec(compact)]
	pub total: BalanceOf<T>,
	#[codec(compact)]
	pub active: BalanceOf<T>,
	pub unlocking: frame_support::BoundedVec<UnlockChunk<BalanceOf<T>>, T::MaxUnlockingChunks>,
	pub legacy_claimed_rewards: frame_support::BoundedVec<sp_staking::EraIndex, T::HistoryDepth>,
}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2270-2295)
```rust
			let (mut member, mut bonded_pool, mut reward_pool) =
				Self::get_member_with_pools(&member_account)?;

			bonded_pool.ok_to_unbond_with(&who, &member_account, &member, unbonding_points)?;

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

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;
```

**File:** substrate/frame/election-provider-multi-phase/test-staking-e2e/src/lib.rs (L347-368)
```rust
		// unbond 2 from pool.
		assert_ok!(Pools::unbond(RuntimeOrigin::signed(2), 2, 10));

		// amount is still locked in the pool, needs to wait for unbonding period.
		assert_eq!(staked_amount_for(pool_bonded_account), 25);

		// max chunks in the ledger are now filled up (`MaxUnlockingChunks == 1`).
		assert_eq!(unlocking_chunks_of(pool_bonded_account), 1);

		// tries to unbond 3 from pool. it will fail since there are no unlocking chunks left
		// available and the current in the queue haven't been there for more than bonding
		// duration.
		assert_err!(
			Pools::unbond(RuntimeOrigin::signed(3), 3, 10),
			pallet_staking::Error::<Runtime>::NoMoreChunks
		);

		assert_eq!(current_era(), 0);

		// progress over bonding duration.
		for _ in 0..=<Runtime as pallet_staking::Config>::BondingDuration::get() {
			start_next_active_era(pool_state.clone()).unwrap();
```
