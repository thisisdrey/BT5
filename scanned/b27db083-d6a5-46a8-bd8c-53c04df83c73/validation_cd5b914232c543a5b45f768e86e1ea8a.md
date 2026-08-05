### Title
Permanent, unrecoverable stall of the Snowbridge Ethereum light client once the finalized-header gap exceeds `SLOTS_PER_HISTORICAL_ROOT` - (File: `bridges/snowbridge/pallets/ethereum-client/src/lib.rs`)

### Summary
The `submit` extrinsic in the `snowbridge-pallet-ethereum-client` enforces a strict, monotonically-worsening constraint between the currently stored finalized header slot and any newly submitted finalized header slot. If no relayer manages to submit an update before the gap between the last stored finalized slot and the current chain head exceeds `SLOTS_PER_HISTORICAL_ROOT` (8192 slots, ~27.3 hours), then *every* future call to `submit` will permanently fail with `InvalidFinalizedHeaderGap`, because the stored slot never advances and the real Ethereum chain only moves forward. This mirrors the Chainlink `roundId`-window bug in the referenced report: a rigid time/slot window check that has no valid value once the gap has grown too large, causing indefinite protocol-wide stall until a privileged `force_checkpoint` (root-only) is issued.

### Finding Description
`verify_update` in `bridges/snowbridge/pallets/ethereum-client/src/lib.rs` contains: [1](#0-0) 

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

`SLOTS_PER_HISTORICAL_ROOT` is a fixed constant (8192) [2](#0-1) , and `latest_finalized_state` is only updated by a successful `submit` call via `store_finalized_header` [3](#0-2) . `submit` is a public, unprivileged, `ensure_signed` extrinsic [4](#0-3) .

The test `submit_finalized_header_update_with_too_large_gap` confirms the check fails once the gap exceeds 8192 slots and produces `InvalidFinalizedHeaderGap` [5](#0-4) .

This is exactly the pattern in the external report: a strict window check (`roundId` must land within `[relevantEpochStartTimestampWithMEWT, relevantEpochStartTimestampWithMEWT + EPOCH_LENGTH)`) that has *no valid satisfying value* once real-world latency/heartbeat gaps exceed the window, permanently freezing forward progress. Here, once `update.finalized_header.slot - latest_finalized_state.slot > 8192`, there is no slot a relayer can submit that will satisfy the `ensure!` — every subsequent slot on the real Ethereum chain is strictly greater than the stored slot, and the stored slot cannot advance without passing this same check. The system enters a state from which no unprivileged actor can recover it; only a root-privileged `force_checkpoint` call can reset the light client [6](#0-5) .

This can be triggered without any malicious actor: any real-world outage of relayer infrastructure, Ethereum beacon-chain congestion, network partition, or simply a lack of active relayers for >27 hours is sufficient — analogous to the Chainlink heartbeat-outlier scenario described in the report (which documented gaps up to 180s against a 27s heartbeat; here the equivalent tolerance window is fixed and unadjustable at 8192 slots regardless of real-world relayer availability).

### Impact Explanation
Once triggered, this permanently halts the Snowbridge Ethereum→Polkadot message verification pipeline: `submit` becomes permanently unusable by any relayer, no new finalized headers or execution headers can be imported, and downstream consumers (inbound message queue, token/asset unlock flows relying on `ExecutionHeader` proofs) stall indefinitely. Recovery requires a privileged governance/root `force_checkpoint` transaction — i.e., bridge state becomes permanently locked for ordinary users/relayers absent an out-of-band administrative intervention. This matches the "permanent user-fund or bridge-state lock" and "public underpriced work that... stalls bridge processing" impact categories.

### Likelihood Explanation
No malicious actor, validator, relayer, or governance compromise is required — the trigger is purely operational: relayer downtime or network congestion exceeding ~27.3 hours (8192 slots) is sufficient. Given the report's own real-world data showing Chainlink Polygon heartbeat outliers of up to 180s against a 27s target (a 6-7x deviation), analogous multi-hour outages of Ethereum relayer infrastructure or beacon-chain finality delays are a realistic, foreseeable operational condition, not a contrived edge case.

### Recommendation
Do not hard-freeze the pallet purely on the finalized-header gap check. Options:
- Allow submission of a fresh `force_checkpoint`-equivalent bootstrap via a permissionless path once the gap is provably unrecoverable (e.g., verified against a trusted execution/consensus proof), rather than requiring root.
- Alternatively, widen the recovery path so that when the gap exceeds `SLOTS_PER_HISTORICAL_ROOT`, the pallet accepts the update by skipping/relaxing the ancestry-proof requirement for the skipped execution headers (analogous to the report's suggested fallback of using the previous valid value instead of reverting entirely), while still validating the new finalized header's own sync-committee signature.
- At minimum, add monitoring/alerting and make the `SLOTS_PER_HISTORICAL_ROOT` gap threshold configurable so operators can add pre-emptive governance action before the terminal state is reached, and document that `force_checkpoint` is the only remedy so operational runbooks account for it.

### Proof of Concept
1. Bootstrap the light client via `force_checkpoint` at slot `S0`.
2. Suspend/omit all `submit` calls (e.g., simulate relayer outage or Ethereum congestion) until the real beacon chain has advanced beyond `S0 + SLOTS_PER_HISTORICAL_ROOT` (8192 slots, ~27.3 hours).
3. Any relayer now calls `submit` with a valid, properly-signed `Update` for the current finalized header (slot `> S0 + 8192`).
4. `verify_update` evaluates:
   ```
   latest_finalized_state.slot (S0) + SLOTS_PER_HISTORICAL_ROOT (8192) >= update.finalized_header.slot
   ```
   This is false for any real subsequent slot, so the call reverts with `Error::InvalidFinalizedHeaderGap`, as reproduced by the existing unit test `submit_finalized_header_update_with_too_large_gap` [5](#0-4) .
5. Because `latest_finalized_state` never advances (it is only updated on a successful `submit`), every subsequent `submit` attempt at any later slot also fails the same check — the pallet is permanently stuck until a root-privileged `force_checkpoint` is executed.

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

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L508-510)
```rust
			if update.finalized_header.slot > latest_finalized_state.slot {
				Self::store_finalized_header(update.finalized_header, update.block_roots_root)?;
			}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/config/mod.rs (L37-38)
```rust
/// The size of the block root array in the beacon state, used for ancestry proofs.
pub const SLOTS_PER_HISTORICAL_ROOT: usize = 8192;
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/tests.rs (L622-651)
```rust
/// Check that a gap of more than 8192 slots between finalized headers is not allowed.
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
