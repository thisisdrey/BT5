Based on my investigation, I found a concrete local analog to the M03 "unnecessary no-op expensive call" bug class in the Snowbridge Ethereum beacon client pallet, though I want to flag one point I could not fully verify before running out of tool budget (noted below).

### Title
Beacon client `submit` performs full BLS sync-committee signature verification for updates that are no-ops (slot not advanced) - ([File: bridges/snowbridge/pallets/ethereum-client/src/lib.rs])

### Summary
The `EthereumBeaconClient::submit` extrinsic always runs `verify_update` (including the expensive `fast_aggregate_verify` BLS signature check) before `apply_update` decides whether to actually store anything. The "is this update useful" gate in `verify_update` (`IrrelevantUpdate`) is evaluated against `update.attested_header.slot`, while the actual state mutation in `apply_update` only happens when `update.finalized_header.slot > latest_finalized_state.slot`. Because the protocol guarantees only `attested_header.slot >= finalized_header.slot` (not equality), an update can pass the `IrrelevantUpdate` check (attested slot newer) while its `finalized_header.slot` is still <= the already-stored slot, making `apply_update` a pure no-op after the full expensive verification has already run.

### Finding Description [1](#0-0) 
`process_update` unconditionally calls `verify_update` then `apply_update`. [2](#0-1) 
The relevance/irrelevance check in `verify_update` is based on `update.attested_header.slot > latest_finalized_state.slot || update_has_next_sync_committee`, not on `finalized_header.slot`. [3](#0-2) 
The costly `fast_aggregate_verify` BLS signature check runs after that relevance gate, for every accepted update. [4](#0-3) 
`apply_update` only calls `store_finalized_header` "if `update.finalized_header.slot > latest_finalized_state.slot`" — i.e. it is a no-op write when the finalized slot hasn't advanced, exactly mirroring the `pot.rho() == now` no-op condition from the report (drip does nothing, but is still called at full cost).

This is a real, previously-acknowledged bug class in this exact pallet: PR 5671 fixed an almost identical issue where "a malicious relayer could spam the Ethereum client with sync committee updates that have already been imported for the period" by adding `LatestSyncCommitteeUpdatePeriod` tracking so repeat updates are no longer free. [5](#0-4) 

That fix addressed the "next sync committee" free-update spam case specifically, tracked via `LatestSyncCommitteeUpdatePeriod`. The regular finalized-header-advance path, however, still separates the "is it worth verifying" decision (`attested_header.slot`) from the "did it actually help" decision (`finalized_header.slot`), leaving the described mismatch.

### Impact Explanation
Every accepted `submit` call — even ones whose `finalized_header.slot` does not advance chain state — pays the full weight cost of BLS signature verification (`fast_aggregate_verify`) declared via `T::WeightInfo::submit()` / `submit_with_sync_committee()`. If any subset of these no-op calls is classified as free (`Pays::No`) by `check_refundable` (I was unable to fully read the body of `check_refundable` before running out of tool calls, so I cannot confirm with certainty whether this exact no-op path can be marked free), an attacker could repeatedly submit distinct, validly-signed but slot-non-advancing updates and force the chain to repeatedly perform expensive BLS verification at little or no cost — directly matching the "public underpriced work that degrades block production or stalls bridge processing" impact class named in the gate. Even if fee-charged, this is a weight-accounting inefficiency identical in shape to the reported `pot.drip` no-op bug.

### Likelihood Explanation
Moderate-to-uncertain: constructing a valid `Update` with a fresh, correctly-signed `attested_header` (slot advancing) but a `finalized_header.slot` that does not exceed the already-stored slot requires a legitimately signed beacon chain update from the actual sync committee — the attacker cannot forge signatures, but can choose *when* and *which* honestly-produced updates to relay, and can replay/submit many such "attested-but-not-newer-finalized" updates that are honestly signed but redundant. The concrete fee-free exploitability of this specific path is unconfirmed because I could not inspect `check_refundable` fully.

### Recommendation
Align the "is this update useful" gate in `verify_update` with the actual state-mutation condition in `apply_update`: require `update.finalized_header.slot > latest_finalized_state.slot` (or the next-sync-committee condition) *before* performing `fast_aggregate_verify`, and ensure `check_refundable` never marks a call free unless `store_finalized_header` (or sync-committee update) actually occurs — following the same pattern already applied for `LatestSyncCommitteeUpdatePeriod` in PR 5671.

### Proof of Concept
1. Bootstrap the beacon client via `force_checkpoint` at finalized slot `S`.
2. Obtain (from the real network) a validly signed `Update` whose `attested_header.slot > S` (passes `IrrelevantUpdate`) but whose `finalized_header.slot <= S` (e.g., finality lagging behind the attested head, or a duplicate finalized checkpoint bundled with a newer attested header).
3. Call `submit` with this update: `verify_update` performs full BLS verification and merkle checks (all succeed), `apply_update` skips `store_finalized_header` because `finalized_header.slot` did not advance — full verification cost paid for zero state change.
4. Repeat with distinct honestly-signed updates meeting the same slot pattern to repeatedly force expensive verification with no chain benefit.

### Citations

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L302-305)
```rust
		pub(crate) fn process_update(update: &Update) -> DispatchResultWithPostInfo {
			Self::verify_update(update)?;
			Self::apply_update(update)
		}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L338-348)
```rust
			// Verify update is relevant.
			let update_attested_period = compute_period(update.attested_header.slot);
			let update_finalized_period = compute_period(update.finalized_header.slot);
			let update_has_next_sync_committee = !<NextSyncCommittee<T>>::exists() &&
				(update.next_sync_committee_update.is_some() &&
					update_attested_period == store_period);
			ensure!(
				update.attested_header.slot > latest_finalized_state.slot ||
					update_has_next_sync_committee,
				Error::<T>::IrrelevantUpdate
			);
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L436-461)
```rust
			// Verify sync committee aggregate signature.
			let sync_committee = if signature_period == store_period {
				<CurrentSyncCommittee<T>>::get()
			} else {
				<NextSyncCommittee<T>>::get()
			};
			let absent_pubkeys =
				Self::find_pubkeys(&participation, (*sync_committee.pubkeys).as_ref(), false);
			let signing_root = Self::signing_root(
				&update.attested_header,
				Self::validators_root(),
				update.signature_slot,
			)?;
			// Improvement here per <https://eth2book.info/capella/part2/building_blocks/signatures/#sync-aggregates>
			// suggested start from the full set aggregate_pubkey then subtracting the absolute
			// minority that did not participate.
			fast_aggregate_verify(
				&sync_committee.aggregate_pubkey,
				&absent_pubkeys,
				signing_root,
				&update.sync_aggregate.sync_committee_signature,
			)
			.map_err(|e| Error::<T>::BLSVerificationFailed(e))?;

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L468-513)
```rust
		fn apply_update(update: &Update) -> DispatchResultWithPostInfo {
			let latest_finalized_state =
				FinalizedBeaconState::<T>::get(LatestFinalizedBlockRoot::<T>::get())
					.ok_or(Error::<T>::NotBootstrapped)?;

			let pays_fee = Self::check_refundable(update, latest_finalized_state.slot);
			let actual_weight = match update.next_sync_committee_update {
				None => T::WeightInfo::submit(),
				Some(_) => T::WeightInfo::submit_with_sync_committee(),
			};

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

			if update.finalized_header.slot > latest_finalized_state.slot {
				Self::store_finalized_header(update.finalized_header, update.block_roots_root)?;
			}

			Ok(PostDispatchInfo { actual_weight: Some(actual_weight), pays_fee })
		}
```

**File:** prdoc/1.16.0/pr_5671.prdoc (L1-16)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Snowbridge free consensus updates border condition fix

doc:
  - audience: Runtime Dev
    description: |
      A fix for a border condition introduced with the Ethereum client free consensus updates. A malicious relayer could
      spam the Ethereum client with sync committee updates that have already been imported for the period. This PR adds
      a storage item to track the last imported sync committee period, so that subsequent irrelevant updates are not free.
      No impact for users or relayers, since the feature introducing the border condition has not been released.

crates:
  - name: snowbridge-pallet-ethereum-client
    bump: patch
```
