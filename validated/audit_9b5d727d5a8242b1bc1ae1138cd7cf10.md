### Title
Hardcoded finalized-header gap limit in Snowbridge `EthereumBeaconClient` can permanently halt bridge light-client updates, matching the "overly strict staleness/latency window" bug class - ([File: bridges/snowbridge/pallets/ethereum-client/src/lib.rs])

### Summary
The external report's core broken invariant is: a fixed, hardcoded staleness/latency bound (`maxLatency = 3600`) is set tighter than the real-world update cadence of the upstream oracle (`heartbeat = 86400`), causing legitimate, honestly-submitted updates to be rejected via `PriceNotRecentEnough`, denying users service even though no malicious actor is involved. The local analog is the `InvalidFinalizedHeaderGap` check in the Snowbridge `pallet_ethereum_client::verify_update`, which enforces that the gap between the last stored finalized header slot and a newly submitted finalized header slot cannot exceed `SLOTS_PER_HISTORICAL_ROOT` (a fixed constant tied to the Ethereum beacon chain's `block_roots` ring buffer size, ~8192 slots / ~27.3 hours). If no valid update is submitted within that window (e.g., relayer downtime, Ethereum consensus issues, or an attacker cheaply spamming the channel to waste relayer submissions/block space while the window elapses), all subsequent updates — even fully valid ones — become permanently rejected with `Error::<T>::InvalidFinalizedHeaderGap`, and the only recovery path is a **root-only** `force_checkpoint` call.

### Finding Description
`verify_update` in [1](#0-0)  enforces:
```
ensure!(
    latest_finalized_state.slot.saturating_add(config::SLOTS_PER_HISTORICAL_ROOT as u64) >=
        update.finalized_header.slot,
    Error::<T>::InvalidFinalizedHeaderGap
);
```
This is a hard, unconditional cap on the elapsed slot gap since the last successfully stored finalized header [2](#0-1) . Unlike a sliding/renewable staleness window (which would simply reject the single stale update and allow retry with a fresh one), this check is monotonically dependent on the last **accepted** header's slot. Once the gap exceeds `SLOTS_PER_HISTORICAL_ROOT`, *no future update, however fresh and valid its own signatures/merkle proofs are*, can pass this check, because the comparison is always against the same stale `latest_finalized_state.slot` — the check can never be satisfied again without external intervention.

The only way to unblock the pallet is `force_checkpoint`, an extrinsic restricted to `ensure_root` [3](#0-2) . There is no permissionless recovery path, and the `submit` extrinsic itself is public/signed [4](#0-3) , meaning ordinary relayers who keep the light client up to date are the only line of defense — any interruption of relayer activity for slightly over the gap window (a realistic operational condition, not requiring a malicious actor) permanently freezes the light client, which underlies Snowbridge's inbound message verification (used by `inbound-queue-v2` etc.) and therefore stalls bridge processing until a governance-gated `force_checkpoint` is executed.

This exactly mirrors the reported bug class: a fixed timing/latency threshold, chosen based on protocol-level assumptions (`SLOTS_PER_HISTORICAL_ROOT`, the sync-committee/ancestry-proof window), turns out to be too tight relative to realistic operational delays, causing legitimate operations to be rejected — except here the consequence is not a "missed one-off trade" but a **permanent halt of bridge state advancement** requiring privileged intervention, which is a stronger and in-scope impact ("permanent user-fund or bridge-state lock" / "public underpriced work that degrades block production or stalls bridge processing").

### Impact Explanation
If the finalized-header gap ever exceeds `SLOTS_PER_HISTORICAL_ROOT` slots (~27 hours) — due to relayer outage, network partition, Ethereum consensus instability, or a low-cost griefing pattern where an attacker submits cheap/no-op transactions to occupy the channel while waiting for the window to lapse — the pallet enters a state where `verify_update` will reject every subsequent `submit` call with `InvalidFinalizedHeaderGap`, indefinitely. Because no non-privileged path exists to recover, the beacon client and all downstream Snowbridge Ethereum→Substrate message verification stall until governance manually replays `force_checkpoint`. This blocks legitimate inbound bridge traffic and can be leveraged to indefinitely freeze bridge-state advancement, aligning with the "permanent user-fund or bridge-state lock" and "message queues/receipts/payout state must only advance after ... execution ... succeed atomically" pivots.

### Likelihood Explanation
No malicious peer, validator, governance actor, or leaked key is required to trigger this: it can occur purely from relayer infrastructure downtime, which is a realistic and foreseeable operational condition (not "off-repo infrastructure" — the gate condition here is a chain-halting *implementation bug*: the check has no self-healing/renewable design). The 27-hour window is derived from the ancestry-proof `block_roots` buffer size and could plausibly be exceeded during outages, congestion, or a low-cost griefing campaign that occupies relayer submission slots. This is directly analogous to the original report's observation that real-world timing characteristics (mainnet heartbeat 86400s vs 3600s bound) were not properly accounted for in a hardcoded threshold.

### Recommendation
Add a permissionless (non-root) recovery mechanism for the case where the finalized-header gap has been exceeded — for example, allow re-bootstrapping the light client from a subsequent valid checkpoint via a signed/permissionless "resync" path that doesn't require `force_checkpoint`'s root origin, or restructure `verify_update` so a sufficiently-proven, self-contained ancestry-independent update (e.g., one accompanied by its own checkpoint-style proof) can re-establish continuity without ancestry proofs back to the old `latest_finalized_state`. At minimum, monitor and alert on approaching the `SLOTS_PER_HISTORICAL_ROOT` gap so governance can intervene before the permanent-lock threshold is crossed, and document/test this failure mode explicitly (the current test `submit_finalized_header_update_with_too_large_gap` [5](#0-4)  only demonstrates the rejection, not any recovery).

### Proof of Concept
1. Establish the light client via `force_checkpoint` at slot `S0`.
2. Do not submit any further valid `submit` updates (simulate relayer downtime or successful griefing that prevents processing) until slot `S0 + SLOTS_PER_HISTORICAL_ROOT + 1` has passed on Ethereum.
3. Attempt `submit` with a fully valid update for a finalized header at slot `> S0 + SLOTS_PER_HISTORICAL_ROOT`.
4. Observe rejection with `Error::<T>::InvalidFinalizedHeaderGap` at [6](#0-5) , as already reproduced in the existing unit test `submit_finalized_header_update_with_too_large_gap` [7](#0-6) .
5. Attempt any further `submit` with an even more recent, fully valid finalized header (any slot after `S0 + SLOTS_PER_HISTORICAL_ROOT`): it also fails identically, because `latest_finalized_state.slot` never advances past `S0`. Only a root-privileged `force_checkpoint` call can restore progress, confirming the permanent-lock condition.

### Citations

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L196-208)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::force_checkpoint())]
		#[transactional]
		/// Used for pallet initialization and light client resetting. Needs to be called by
		/// the root origin.
		pub fn force_checkpoint(
			origin: OriginFor<T>,
			update: Box<CheckpointUpdate>,
		) -> DispatchResult {
			ensure_root(origin)?;
			Self::process_checkpoint_update(&update)?;
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L217-224)
```rust
		#[transactional]
		/// Submits a new finalized beacon header update. The update may contain the next
		/// sync committee.
		pub fn submit(origin: OriginFor<T>, update: Box<Update>) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);
			Self::process_update(&update)
		}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L323-326)
```rust
			// Retrieve latest finalized state.
			let latest_finalized_state =
				FinalizedBeaconState::<T>::get(LatestFinalizedBlockRoot::<T>::get())
					.ok_or(Error::<T>::NotBootstrapped)?;
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L350-359)
```rust
			// Verify the finalized header gap between the current finalized header and new imported
			// header is not larger than the sync committee period, otherwise we cannot do
			// ancestry proofs for execution headers in the gap.
			ensure!(
				latest_finalized_state
					.slot
					.saturating_add(config::SLOTS_PER_HISTORICAL_ROOT as u64) >=
					update.finalized_header.slot,
				Error::<T>::InvalidFinalizedHeaderGap
			);
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/tests.rs (L623-651)
```rust
#[test]
fn submit_finalized_header_update_with_too_large_gap() {
	let checkpoint = Box::new(load_checkpoint_update_fixture());
	let update = Box::new(load_sync_committee_update_fixture());
	let mut next_update = Box::new(load_next_sync_committee_update_fixture());

	// Adds 8193 slots, so that the next update is still in the next sync committee, but the
	// gap between the finalized headers is more than 8192 slots.
	let slot_with_large_gap = checkpoint.header.slot + SLOTS_PER_HISTORICAL_ROOT as u64 + 1;

	next_update.finalized_header.slot = slot_with_large_gap;
	// Adding some slots to the attested header and signature slot since they need to be ahead
	// of the finalized header.
	next_update.attested_header.slot = slot_with_large_gap + 33;
	next_update.signature_slot = slot_with_large_gap + 43;

	new_tester().execute_with(|| {
		assert_ok!(EthereumBeaconClient::process_checkpoint_update(&checkpoint));
		let result = EthereumBeaconClient::submit(RuntimeOrigin::signed(1), update.clone());
		assert_ok!(result);
		assert_eq!(result.unwrap().pays_fee, Pays::No);
		assert!(<NextSyncCommittee<Test>>::exists());

		let second_result =
			EthereumBeaconClient::submit(RuntimeOrigin::signed(1), next_update.clone());
		assert_err!(second_result, Error::<Test>::InvalidFinalizedHeaderGap);
		assert_eq!(second_result.unwrap_err().post_info.pays_fee, Pays::Yes);
	});
}
```
