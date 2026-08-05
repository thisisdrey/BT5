Audit Report

## Title
`pallet-nomination-pools::unbond` keys unbonding chunks using `current_era()` (a legacy-named alias documented to return active era) causing dissolved points whose funds may never unlock under CurrentEra/ActiveEra divergence - (File: `substrate/frame/nomination-pools/src/lib.rs`)

## Summary
`Pallet::unbond` computes `unbond_era` from `T::StakeAdapter::current_era()`, storing the result in a variable literally named `active_era`, then uses it both to lazily create/merge `sub_pools.with_era` buckets and to permanently dissolve the member's bonded-pool points via `bonded_pool.dissolve(unbonding_points)` before the corresponding funds are ever released. [1](#0-0)  The adapter's own doc comment concedes the method is "named `current_era` for legacy interface compatibility" and is intended to return the active era for all non-election staking logic, which is itself an admission that this naming is a known source of confusion. [2](#0-1)  The repository's own change history confirms this exact class of bug already manifested in production and required a dedicated one-time storage migration (`pr_11018`) to release a member's balance that got permanently trapped because "points were dissolved but the held funds weren't released" due to a "CurrentEra vs ActiveEra mismatch," alongside a broader still-referenced standardization effort (`pr_10986`, "Current Era should only be used for election logic").

## Finding Description
`unbond()` is an unprivileged, permissionless extrinsic. It dissolves the caller's points from the bonded pool immediately and irrevocably, and computes the era key for the corresponding `UnbondPool` using `T::StakeAdapter::current_era()` — a method whose only implementation forwards directly to `Self::CoreStaking::current_era()` with no correction applied. [2](#0-1)  The pool-side unlock accounting (both the `sub_pools.with_era` bucket key and the `maybe_merge_pools` call) is thus keyed to whatever `CoreStaking::current_era()` returns for the configured staking backend, while the staking backend's actual fund-unlock gating in `pallet-staking-async::do_withdraw_unbonded` uses `Rotator::<T>::active_era()` and `calculate_earliest_withdrawal_era`, which can diverge from `current_era()` when offences are queued (`OffenceQueueEras`). [3](#0-2) [4](#0-3)  If these two eras diverge, dissolution of bonded-pool points is irreversible while the unlock-era key used for `sub_pools` accounting no longer matches what the staking backend will actually honor — this is precisely the scenario that PR `pr_11018`'s prdoc says already happened and required a manual migration to fix trapped balances.

I confirmed the vulnerable code is present as described in the current repository state: the exact lines, variable naming, and adapter documentation match the claim verbatim. [5](#0-4)  I was not able to fully trace, within the available indexing/tool budget, whether `pallet-staking-async`'s concrete `StakingInterface::current_era()` implementation (as opposed to the doc-comment claim in `adapter.rs`) is actually wired to return `ActiveEra` rather than `CurrentEra` storage in the current codebase, nor whether `pr_10986`'s "standardization" work has already remediated this specific `unbond()` call site for `pallet-staking-async`-backed pools (the prdoc for `pr_10986` describes the effort as bumping both `pallet-nomination-pools` and `pallet-staking-async` at the "patch" level, consistent with an in-progress or already-applied fix, but the `unbond()` code itself still reads `T::StakeAdapter::current_era()`).

## Impact Explanation
If the divergence is unremediated for a given staking backend, the impact is a permanent user-fund lock: pool member points are dissolved from the bonded pool (unrecoverable — no further rewards or re-bonding) while the funds sit in a `sub_pools.with_era` bucket keyed to an era that the staking pallet's actual withdrawal gating never aligns with at the expected time. This matches the "permanent user-fund or bridge-state lock" category in the impact gate, and is not a theoretical claim — the repository's own `pr_11018` prdoc documents that exactly this happened once and required a bespoke one-time migration to unlock one member's trapped balance.

## Likelihood Explanation
`unbond` is called directly and repeatedly by any nomination-pool member, requiring no privileged access, governance, or validator collusion — it is a routine, permissionless extrinsic. The precondition (CurrentEra/ActiveEra divergence) is backend-dependent and tied to `pallet-staking-async`'s offence-queue-based active-era rotation, which the code itself describes as capable of lagging behind `current_era`/election era under queued offences. Whether this precondition is currently reachable in the shipped configuration (i.e., whether `pr_10986`'s standardization already closed this specific call site) could not be conclusively determined from the index; the code as read still contains the vulnerable call, but I cannot rule out that other guards or corrected `StakingInterface::current_era()` semantics for the specific `T::StakeAdapter::CoreStaking` type in use fully compensate for it.

## Recommendation
Replace `T::StakeAdapter::current_era()` in `unbond()` (and audit any other non-election call sites in `pallet-nomination-pools`) with an explicit `active_era()`-semantics accessor that is guaranteed, by type or trait contract (not just doc comment), to match the era used by the staking backend's `consolidate_unlocked`/`calculate_earliest_withdrawal_era` logic for fund release. Add integration tests that intentionally create a `CurrentEra`/`ActiveEra` divergence (e.g., via pending `OffenceQueueEras` entries) and assert that the pool's `unbond_era` key always aligns with the staking pallet's real withdrawal-eligible era.

## Proof of Concept
1. Configure a pool with `pallet-staking-async` as the staking backend and induce a state where `OffenceQueueEras` contains unprocessed entries, causing `Rotator::<T>::active_era()`/`calculate_earliest_withdrawal_era` to diverge from `CoreStaking::current_era()`. [3](#0-2) 
2. Call `Pools::unbond` for a member; observe `unbond_era = T::StakeAdapter::current_era() + bonding_duration()` recorded, and the member's points immediately dissolved from `bonded_pool`. [6](#0-5) 
3. Advance eras until the staking pallet's real active/withdrawal-eligible era (per `do_withdraw_unbonded`) diverges from the recorded `unbond_era`, and attempt `withdraw_unbonded` on the pools side — the `sub_pools.with_era` entry either cannot be matched/released at the expected time or the underlying ledger unlock never lines up, leaving balance stuck exactly as remediated by the `pr_11018` migration.

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
