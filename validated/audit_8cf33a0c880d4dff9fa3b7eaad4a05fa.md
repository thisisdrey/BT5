# Finding: Snowbridge Ethereum light client accepts and rewards "no-op" sync-committee submissions as free, heavy-weight work

### Title
Silent no-op `next_sync_committee_update` is accepted, evented, and eligible for fee-free dispatch, enabling underpriced repeated BLS-verification spam - (File: `bridges/snowbridge/pallets/ethereum-client/src/lib.rs`)

### Summary
This is the same class of bug as the ChainLink/Tellor report: a secondary/fallback branch of a state machine performs strictly weaker validation than the primary branch, but the caller still treats it as a fully-validated, "free" state transition. In the Snowbridge beacon light client, `apply_update` has a branch that is entered whenever a submitted update carries a `next_sync_committee_update`, but that branch silently does nothing to storage when the update doesn't actually advance the sync-committee window - yet it still unconditionally emits `Event::SyncCommitteeUpdated`, unconditionally bumps `LatestSyncCommitteeUpdatePeriod`, and is dispatched under the heavier `submit_with_sync_committee()` weight/fee-exemption path introduced for legitimate committee rotations (`pr_4102`/`pr_5671`, "free consensus updates").

### Finding Description
`verify_update` gates period skipping with: [1](#0-0) 
which permits `signature_period` to equal either `store_period` or `store_period + 1` whenever `NextSyncCommittee` already exists. This is broader than the state transitions `apply_update` actually knows how to apply.

`apply_update` then does: [2](#0-1) 

Note the two guarded branches:
- `!NextSyncCommittee::exists()` → sets `NextSyncCommittee`
- `update_finalized_period == store_period + 1` → rotates `Current`/`Next`

There is **no `else`** for the case where `NextSyncCommittee` already exists and `update_finalized_period` is *not* `store_period + 1` (e.g., it equals `store_period`, i.e., a resubmission of an already-known/committed period that `verify_update`'s relevance/period checks still let through). In that case:
- No storage mutation to `CurrentSyncCommittee`/`NextSyncCommittee` occurs (silent no-op for the actual data being "verified"),
- but the code unconditionally executes `tracing::info!`, sets `LatestSyncCommitteeUpdatePeriod::<T>::set(update_finalized_period)`, and fires `Event::SyncCommitteeUpdated { period: ... }` as if a real committee rotation had been recorded.

This mirrors the Chainlink report's broken invariant exactly: the "fallback" acceptance path (period equal to `store_period` while `NextSyncCommittee` already exists) skips the validation/state-effect that the "primary" path performs (actually storing the new committee), yet the call is still accepted, evented, and — because the extrinsic still carries a `next_sync_committee_update`, it is weighed/charged (or fee-waived, per the free-headers/free-consensus-update design) as `submit_with_sync_committee()`: [3](#0-2) 

The pallet's own history shows this exact fee/period bookkeeping is fragile: `pr_5671` had to add `LatestSyncCommitteeUpdatePeriod` specifically because "a malicious relayer could spam the Ethereum client with sync committee updates that have already been imported for the period," making such spam free. The missing `else` branch reopens a variant of that same class of problem: a call that does full, expensive BLS aggregate-signature verification (`fast_aggregate_verify`, `verify_merkle_branch` for the next-committee Merkle proof) and is billed/exempted at the heavy-weight "with sync committee" rate, while doing no actual committee-state work, and while emitting a misleading `SyncCommitteeUpdated` event that downstream indexers/relayers may treat as a real rotation.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing": an unprivileged relayer can repeatedly submit valid-but-non-advancing updates (same `finalized_header.slot`/period, valid signature over the current or next committee, satisfying all of `verify_update`'s `ensure!` checks) to force the runtime to repeatedly perform the most expensive verification path (`submit_with_sync_committee` weight class, full BLS aggregate verification) while producing zero net light-client progress. If such calls are also fee-exempted by the "free consensus update" mechanism (the mechanism `pr_5671` had to patch once already), this is effectively free, unbounded, heavy computational load on block production, and it pollutes on-chain events with `SyncCommitteeUpdated` notifications that never corresponded to a real update — a false-state-acceptance analog to the price-feed bug (a value is treated as freshly validated/updated when it was not).

### Likelihood Explanation
High: no privileged actor is required. Any account that can call `submit` and construct/relay one valid signed beacon update per period (which relayers must do routinely anyway) can trivially resubmit the same update (or a very slightly different one within the same period window) repeatedly, since `verify_update`'s relevance/skip checks (lines 329-336, 344-348) are wide enough to admit `update_finalized_period == store_period` while `NextSyncCommittee` already exists.

### Recommendation
Add an explicit `else` branch in `apply_update` for the case where `NextSyncCommittee` exists and `update_finalized_period != store_period + 1`: either reject the call (mirroring the strictness the report recommends — don't silently accept an update that doesn't produce the state effect its weight/fee class assumes), or explicitly downgrade it to the cheaper `submit()` weight/fee path with no event emission, so that fee/weight and event emission are strictly tied to an actual verified state transition, closing the gap the same way `pr_5671` closed the prior free-update border condition.

### Proof of Concept
1. Bootstrap the light client via `force_checkpoint`, then submit a valid `Update` containing a `next_sync_committee_update` for the current period, populating `NextSyncCommittee` (this is the normal `submit_update_with_skipped_period`/`submit_finalized_header_update_with_gap_at_limit`-style fixture flow already exercised in `bridges/snowbridge/pallets/ethereum-client/src/tests.rs`).
2. Construct a second `Update` whose `finalized_header.slot` maps to the *same* `store_period` (not `store_period + 1`), but whose `attested_header.slot` is strictly greater than the currently stored finalized slot (satisfying the `IrrelevantUpdate` check) and which re-includes a (possibly identical) `next_sync_committee_update` with a valid Merkle branch and a validly BLS-signed `sync_aggregate` from the appropriate committee (current or next, depending on `signature_period`).
3. Call `submit` with this second update. `verify_update` passes (period checks in lines 329-336, 344-348 are satisfied); `apply_update`'s `if !NextSyncCommittee::exists()` is false and `else if update_finalized_period == store_period + 1` is false, so neither storage-mutating branch runs — yet `LatestSyncCommitteeUpdatePeriod` is set and `Event::SyncCommitteeUpdated` fires, and the call is billed/weighed as `submit_with_sync_committee()`.
4. Repeat step 3 arbitrarily many times per block to generate unbounded heavy-weight, event-emitting, non-progressing calls.

### Citations

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L210-216)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight({
			match update.next_sync_committee_update {
				None => T::WeightInfo::submit(),
				Some(_) => T::WeightInfo::submit_with_sync_committee(),
			}
		})]
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L327-336)
```rust
			let store_period = compute_period(latest_finalized_state.slot);
			let signature_period = compute_period(update.signature_slot);
			if <NextSyncCommittee<T>>::exists() {
				ensure!(
					(store_period..=store_period + 1).contains(&signature_period),
					Error::<T>::SkippedSyncCommitteePeriod
				)
			} else {
				ensure!(signature_period == store_period, Error::<T>::SkippedSyncCommitteePeriod)
			}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L479-506)
```rust
			if let Some(next_sync_committee_update) = &update.next_sync_committee_update {
				let store_period = compute_period(latest_finalized_state.slot);
				let update_finalized_period = compute_period(update.finalized_header.slot);
				let sync_committee_prepared: SyncCommitteePrepared = (&next_sync_committee_update
					.next_sync_committee)
					.try_into()
					.map_err(|_| <Error<T>>::BLSPreparePublicKeysFailed)?;

				if !<NextSyncCommittee<T>>::exists() {
					ensure!(
						update_finalized_period == store_period,
						<Error<T>>::InvalidSyncCommitteeUpdate
					);
					<NextSyncCommittee<T>>::set(sync_committee_prepared);
				} else if update_finalized_period == store_period + 1 {
					<CurrentSyncCommittee<T>>::set(<NextSyncCommittee<T>>::get());
					<NextSyncCommittee<T>>::set(sync_committee_prepared);
				}
				tracing::info!(
					target: LOG_TARGET,
					period=%update_finalized_period,
					"💫 SyncCommitteeUpdated."
				);
				<LatestSyncCommitteeUpdatePeriod<T>>::set(update_finalized_period);
				Self::deposit_event(Event::SyncCommitteeUpdated {
					period: update_finalized_period,
				});
			};
```
