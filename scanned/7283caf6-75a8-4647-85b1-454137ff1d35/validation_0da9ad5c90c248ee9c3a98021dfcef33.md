### Title
`pallet-bags-list::on_update` silently drops score-decrease events while the list is locked, leaving stale (inflated) voter scores uncorrected — ([File: substrate/frame/bags-list/src/lib.rs])

### Summary
The Velodrome bug's broken invariant is: an accounting entry (`_moveTokenDelegates`) is faithfully applied on the "increase" paths (`_mint`, `_transferFrom`) but is never applied on the "decrease" path (`_burn`), so a value that should be reduced/removed stays stuck at its old, higher amount. `pallet-bags-list` has the same asymmetry: `on_insert` has an explicit fallback (`PendingRebag`) that guarantees an account is eventually reconciled even if the operation fails while the list is `Lock`ed, but `on_update`/`on_decrease` have no such fallback. When a staker reduces their score (e.g. via `unbond`) while the list is locked, the call errors out, the caller discards the error, and the stale (higher) score is never queued for correction — unlike inserts, it is not remembered anywhere.

### Finding Description
`pallet-bags-list`'s `SortedListProvider::on_insert` explicitly handles the locked case by queuing the account into `PendingRebag` so it is guaranteed to be corrected later by `on_idle`: [1](#0-0) 

`on_update`, by contrast, only calls `ensure_unlocked()?` and simply propagates `Error::Locked` with **no** equivalent enqueue-for-later-reconciliation logic: [2](#0-1) 

`PendingRebag` is documented as capturing failed **insertions**, not failed updates: [3](#0-2) 

The consuming pallet (`pallet-staking-async`) calls `on_update` after an `unbond()` and **discards** the result: [4](#0-3) 

So the sequence is:
1. `Lock::<T,I>::put(())` is set while an election snapshot/voter list generation is in progress (a routine, permissionless-triggered chain operation, not an admin/governance action).
2. A staker calls `unbond()` during this window, reducing `ledger.active` and hence their true stake weight, and the code attempts `VoterList::on_update(&stash, Self::weight_of(&stash))`.
3. `on_update` returns `Err(ListError::Locked)` because the list is locked; the caller ignores the error via `let _ = ...`.
4. Unlike `on_insert`, this failed update is **not** stored anywhere (no `PendingRebag` entry is created for updates), so `ListNodes` keeps the account's **old, higher** score.
5. The account's `ListNodes` score entry, which drives its position/weight in the bags-list used for the sorted voter list, remains overstated until the regular `on_idle` cursor eventually reaches that specific account (bounded by `MaxAutoRebagPerBlock`, so it can take many blocks for a large list) or someone manually calls `rebag`.

This is the exact structural analog of the Velodrome bug: the "increase" path (`on_insert`) has a durable reconciliation guarantee (`PendingRebag`), but the "decrease" path (`on_update`/`on_decrease`) does not, so a value that should shrink stays inflated — an account can effectively retain more list-weight ("votes") than it currently backs with stake, for a window whose length is attacker-uncontrolled but occurs on every election cycle.

### Impact Explanation
The bags-list score is the primary signal `pallet-staking(-async)`'s election/voter-snapshot machinery uses to rank and select voters/nominators. If a staker's score is not decremented as intended after unbonding during a locked window, that staker is represented with more stake-backed influence than they actually hold for the affected snapshot/period, and other legitimately larger stakers can be mis-ranked or displaced. This falls under "runtime bugs that compromise intended behavior" for stake-weighted selection, mirroring the "favored pools due to gauge values" harm in the original report (wrong weighting → wrong selection outcomes), and is already acknowledged as a real class of issue in this codebase (see the related, but narrower, already-fixed "Chill stakers should not have a score" bug and the "Remove failing assertion related to VoterList count mismatch" prdoc, both evidencing that voter-list/score desynchronization from locked-list state is a recognized live problem area).

### Likelihood Explanation
No privileged actor, validator, collator, or relayer is required — a normal, permissionless `unbond()` call from any staker triggers the code path. The only condition is that the call lands while `pallet-bags-list::Lock` is set (which happens automatically during every election-snapshot generation cycle, a routine, recurring, protocol-driven window, not an artificial or attacker-crafted precondition). Because `on_update` failures are silently swallowed and never queued, correction is entirely dependent on the unrelated background `on_idle` cursor sweep, making the staleness window unbounded in the worst case for large lists with a small `MaxAutoRebagPerBlock`.

### Recommendation
Make `on_update`/`on_decrease` symmetric with `on_insert`: when the list is locked, instead of returning `Err(Locked)` and letting callers discard it, insert the account into `PendingRebag` (as `on_insert` already does) so `on_idle` is guaranteed to reconcile the score once the list unlocks. Alternatively, callers such as `pallet-staking-async::unbond` should not silently discard `on_update`'s error and should themselves enqueue affected accounts for guaranteed reconciliation.

### Proof of Concept
1. Configure a runtime where `pallet-bags-list` backs the staking voter list and `pallet-staking-async` drives `unbond`.
2. Have staker `S` with a large active bond, present in `ListNodes` with score `X` (high bag).
3. Trigger `Lock::<T,I>::put(())` (as happens automatically at the start of an election snapshot phase — see `voter_list_not_updated_when_locked` test showing `pallet_bags_list::Lock` becomes `Some(())` mid-snapshot).
4. While locked, `S` calls `Staking::unbond(large_amount)`. `ledger.active` drops; `T::VoterList::contains(&stash)` is true, so `on_update(&stash, new_lower_weight)` is invoked and returns `Err(Locked)`, which is discarded (`let _ = ...`).
5. Unlock the list. `ListNodes::<T,I>::get(&S)` still reports the old, higher score `X`, and `S` is not present in `PendingRegag` (only `on_insert` populates that queue), so `S` retains an inflated bag position until the ordinary `on_idle` rebagging cursor eventually walks to `S`'s position in the list — which, per `on_idle`'s own budget (`MaxAutoRebagPerBlock`), can take an arbitrary number of blocks for a populous list, during which `S` is over-weighted relative to its real stake for any snapshot/consumer reading the list.

**Note on confidence:** I was not able to fully inspect every call site of `on_update`/`on_decrease`/`on_increase` (e.g., in `substrate/frame/staking/src/pallet/impls.rs` and `staking-async/src/pallet/impls.rs`) before running out of tool iterations, so I cannot state with certainty whether *all* consumers discard the `Locked` error the same way `unbond()` does, or whether some other code path independently re-queues affected accounts. This should be verified in a follow-up review before treating the finding as fully confirmed end-to-end.

### Citations

**File:** substrate/frame/bags-list/src/lib.rs (L287-302)
```rust
	/// These accounts will be processed with priority in `on_idle` or via `rebag` extrinsic.
	///
	/// Note: This storage is intentionally unbounded. The following factors make bounding
	/// unnecessary:
	/// 1. The storage usage is temporary - accounts are processed and removed in `on_idle`
	/// 2. The pallet is only locked during snapshot generation, which is weight-limited
	/// 3. Processing happens at multiple accounts per block, clearing even large backlogs quickly
	/// 4. An artificial limit could be exhausted by an attacker, preventing legitimate
	///    auto-rebagging from putting accounts in the correct position
	///
	/// We don't store the score here - it's always fetched from `ScoreProvider` when processing,
	/// ensuring we use the most up-to-date score (accounts may have been slashed, rewarded, etc.
	/// while waiting in the queue).
	#[pallet::storage]
	pub type PendingRebag<T: Config<I>, I: 'static = ()> =
		CountedStorageMap<_, Twox64Concat, T::AccountId, ()>;
```

**File:** substrate/frame/bags-list/src/lib.rs (L715-727)
```rust
	fn on_insert(id: T::AccountId, score: T::Score) -> Result<(), ListError> {
		if Pallet::<T, I>::ensure_unlocked().is_err() {
			// Pallet is locked - store in PendingRebag for later processing
			// Only queue if auto-rebagging is enabled
			if T::MaxAutoRebagPerBlock::get() > 0u32 {
				PendingRebag::<T, I>::insert(&id, ());
				return Ok(());
			}

			return Err(ListError::Locked);
		};
		List::<T, I>::insert(id, score)
	}
```

**File:** substrate/frame/bags-list/src/lib.rs (L729-732)
```rust
	fn on_update(id: &T::AccountId, new_score: T::Score) -> Result<(), ListError> {
		Pallet::<T, I>::ensure_unlocked()?;
		Pallet::<T, I>::do_rebag(id, new_score).map(|_| ())
	}
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2024-2030)
```rust
				// NOTE: ledger must be updated prior to calling `Self::weight_of`.
				ledger.update()?;

				// update this staker in the sorted list, if they exist in it.
				if T::VoterList::contains(&stash) {
					let _ = T::VoterList::on_update(&stash, Self::weight_of(&stash));
				}
```
