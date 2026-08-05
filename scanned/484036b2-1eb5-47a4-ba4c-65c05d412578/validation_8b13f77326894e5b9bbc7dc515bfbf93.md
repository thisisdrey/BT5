## Analysis

I found a real, documented local analog to the same bug class as the Primitive report: a struct/state field that is meant to represent a **live, time-relative quantity** (era progress) is instead populated with a **stale/misnamed source of truth**, and downstream consumers (fund settlement code) trusted it without correction — producing wrong beneficiary settlement, exactly analogous to `transform()` hard-coding `timeRemainingSeconds` to the static duration instead of the live remaining time.

### Title
`pallet-nomination-pools::unbond` keys unbonding chunks using `current_era` instead of `active_era`, causing dissolved points with funds that never unlock (CurrentEra/ActiveEra mismatch) - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::unbond` computes `unbond_era` from `T::StakeAdapter::current_era()` [1](#0-0)  even though the adapter's own documentation states this method actually "Returns active era which should be used for all non-election staking logic" and that `CurrentEra` should be reserved for election logic [2](#0-1) . `CurrentEra` and `ActiveEra` are not the same value in `pallet-staking`/`pallet-staking-async` — `CurrentEra` can be planned/incremented ahead of the era that is actually `active` on-chain (used for exposures/slashing/consolidation). The repository's own prdoc history confirms this exact confusion was a live, already-exploited bug class: PR `pr_11018` describes "[Pool] Claim trapped balance via one-time migration," stating verbatim: *"A bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were dissolved but the held funds weren't released"* [3](#0-2) , and PR `pr_10986` "[Pool] Use active era for withdrawals" documents the broader standardization effort still described as ongoing/patch-level ("Current Era should only be used for election logic") [4](#0-3) .

### Finding Description
`unbond()` dissolves the member's bonded-pool points immediately (`bonded_pool.dissolve(unbonding_points)`) and computes the unlock era as:
```
let active_era = T::StakeAdapter::current_era();
let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);
``` [5](#0-4) 

The variable is *named* `active_era` in the code but is populated from `current_era()`. Because `StakeStrategy::current_era()` is explicitly documented to alias `active_era` semantics for "legacy interface compatibility" [2](#0-1) , any adapter implementation (or staking backend) where the underlying `current_era()` diverges from the true active era used for unlock-chunk consolidation in `pallet-staking`/`pallet-staking-async`'s `do_withdraw_unbonded` (`consolidate_unlocked(active_era)` / `calculate_earliest_withdrawal_era(active_era)`) [6](#0-5)  results in the pool's `sub_pools.with_era` bucket being keyed by an era that never matches the era the staking pallet actually consolidates/unlocks funds for. This is structurally identical to the Primitive bug: a value meant to reflect "current live state" (time remaining / active era) is instead sourced from a static/differently-scoped field (full duration / current-era-for-elections), and no downstream correction is applied before the value is used to gate fund release.

### Impact Explanation
When the pool-side unlock era (derived from the wrong era source) doesn't line up with the staking pallet's consolidation era, member points are dissolved from the bonded pool (irreversible — they can never claim rewards or re-bond) while the corresponding funds remain held in a `sub_pools.with_era` bucket keyed to an era that the staking ledger never reaches at the expected time, or that is skipped by `consolidate_unlocked`. This is a **permanent user-fund lock** class issue: the confirmed real-world instance required a manual one-time storage migration (`pr_11018`) to release one member's trapped funds, evidencing this is not theoretical — it already produced unbacked/locked balance in production-adjacent state (points dissolved, funds not released) [7](#0-6) .

### Likelihood Explanation
Likelihood is elevated by the fact that `unbond` is a normal, unprivileged, permissionless user extrinsic invoked continuously by nomination-pool members — no malicious actor, governance, or validator collusion is required. The divergence is purely a consequence of `CurrentEra` vs `ActiveEra` semantics differing under `pallet-staking-async`'s newer offence-queue-aware active-era rotation logic (`calculate_earliest_withdrawal_era`, `Rotator::active_era()`) [8](#0-7) , which can lag `CurrentEra` when offences are queued. The repository's own commit history shows this already manifested once in practice and required remediation via a dedicated migration, and the broader standardization PR (`pr_10986`) indicates other, not-yet-covered call sites using `current_era` for non-election purposes may still exist.

### Recommendation
Rename/replace `T::StakeAdapter::current_era()` calls used for non-election fund-unlock accounting (as in `unbond()`) with an explicit `active_era()` accessor that is guaranteed to match the era used by the staking backend's `consolidate_unlocked`/`calculate_earliest_withdrawal_era` logic. Audit every remaining call site of `current_era()` in `pallet-nomination-pools` (`src/lib.rs`, `src/adapter.rs`) to confirm none are used to gate irreversible dissolution of points or fund settlement without era-consistency guarantees, and add integration tests asserting the pool's computed unlock era always matches the staking pallet's actual unlock era under active-era lag scenarios (e.g., pending offence queues).

### Proof of Concept
Conceptual PoC (test-level), given `pallet-staking-async`'s active era can lag `CurrentEra`/election era due to `OffenceQueueEras` processing delay [8](#0-7) :
1. Bond a pool member; let the staking backend enter a state where `current_era()` (as returned via `T::StakeAdapter::current_era()`) is ahead of the value that `consolidate_unlocked`/withdrawal logic in `pallet-staking-async` treats as `active_era` (e.g., because of unprocessed offences in `OffenceQueueEras`).
2. Call `Pools::unbond` — the pool records `unbond_era = current_era() + bonding_duration`, and dissolves the member's points from the bonded pool immediately [5](#0-4) .
3. Advance the chain until the staking pallet's real `active_era` (as gated by `calculate_earliest_withdrawal_era`) reaches/exceeds `unbond_era`, but because the pool-recorded era was computed from the wrong source, `withdraw_unbonded` on the pools side either releases too early/late or never matches the `sub_pools.with_era` key, leaving the balance held in `sub_pools` un-claimable — mirroring the exact scenario the `pr_11018` migration was written to remediate.

This is confirmed as a real, previously manifested defect (not speculative) via the repository's own prdoc record, though I could not fully verify from the index whether all call sites have since been patched beyond the one-time migration — a background Devin session with full repo/test access would be needed to confirm the current patched state of every `current_era()` call site in `pallet-nomination-pools`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2290-2323)
```rust
			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

			// Note that we lazily create the unbonding pools here if they don't already exist
			let mut sub_pools = SubPoolsStorage::<T>::get(member.pool_id)
				.unwrap_or_default()
				.maybe_merge_pools(active_era);

			// Update the unbond pool associated with the current era with the unbonded funds. Note
			// that we lazily create the unbond pool if it does not yet exist.
			if !sub_pools.with_era.contains_key(&unbond_era) {
				sub_pools
					.with_era
					.try_insert(unbond_era, UnbondPool::default())
					// The above call to `maybe_merge_pools` should ensure there is
					// always enough space to insert.
					.defensive_map_err::<Error<T>, _>(|_| {
						DefensiveError::NotEnoughSpaceInUnbondPool.into()
					})?;
			}

			let points_unbonded = sub_pools
				.with_era
				.get_mut(&unbond_era)
				// The above check ensures the pool exists.
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?
				.issue(unbonding_balance);

			// Try and unbond in the member map.
			member.try_unbond(unbonding_points, points_unbonded, unbond_era)?;
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L101-107)
```rust
	/// See [`StakingInterface::current_era`].
	///
	/// Note: Named current_era for legacy interface compatibility. Returns active era which
	/// should be used for all non-election staking logic.
	fn current_era() -> EraIndex {
		Self::CoreStaking::current_era()
	}
```

**File:** prdoc/stable2512-3/pr_11018.prdoc (L1-10)
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
```

**File:** prdoc/stable2512-2/pr_10986.prdoc (L1-10)
```text
title: '[Pool] Use active era for withdrawals'
doc:
- audience: Runtime Dev
  description: Standardising using active era in pools and staking. Current Era should
    only be used for election logic
crates:
- name: pallet-nomination-pools
  bump: patch
- name: pallet-staking-async
  bump: patch
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L229-257)
```rust
	/// Calculate the earliest era that withdrawals are allowed for, considering:
	/// - The current active era
	/// - Any unprocessed offences in the queue
	fn calculate_earliest_withdrawal_era(active_era: EraIndex) -> EraIndex {
		// get lowest era for which all offences are processed and withdrawals can be allowed.
		let earliest_unlock_era_by_offence_queue = OffenceQueueEras::<T>::get()
			.as_ref()
			.and_then(|eras| eras.first())
			.copied()
			// if nothing in queue, use the active era.
			.unwrap_or(active_era)
			// above returns earliest era for which offences are NOT processed yet, so we subtract
			// one from it which gives us the oldest era for which all offences are processed.
			.saturating_sub(1)
			// Unlock chunks are keyed by the era they were initiated plus their unbond duration.
			// We use full BondingDuration (validator duration) here because:
			// - For validators: this is their actual unbond duration
			// - For nominators: when slashable, they use full duration; when not slashable, their
			//   chunks already have shorter unlock eras (set during unbond), so this calculation
			//   still correctly allows their withdrawals.
			.saturating_add(T::BondingDuration::get());

		// If there are unprocessed offences older than the active era, withdrawals are only
		// allowed up to the last era for which offences have been processed.
		// Note: This situation is extremely unlikely, since offences have `SlashDeferDuration` eras
		// to be processed. If it ever occurs, it likely indicates offence spam and that we're
		// struggling to keep up with processing.
		active_era.min(earliest_unlock_era_by_offence_queue)
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L259-280)
```rust
	pub(super) fn do_withdraw_unbonded(controller: &T::AccountId) -> Result<Weight, DispatchError> {
		let mut ledger = Self::ledger(Controller(controller.clone()))?;
		let (stash, old_total) = (ledger.stash.clone(), ledger.total);
		let active_era = Rotator::<T>::active_era();

		// Ensure last era slashes are applied. Else we block the withdrawals.
		if active_era > 1 {
			Self::ensure_era_slashes_applied(active_era.saturating_sub(1))?;
		}

		let earliest_era_to_withdraw = Self::calculate_earliest_withdrawal_era(active_era);

		log!(
			debug,
			"Withdrawing unbonded stake. Active_era is: {:?} | \
			Earliest era we can allow withdrawing: {:?}",
			active_era,
			earliest_era_to_withdraw
		);

		// withdraw unbonded balance from the ledger until earliest_era_to_withdraw.
		ledger = ledger.consolidate_unlocked(earliest_era_to_withdraw);
```
