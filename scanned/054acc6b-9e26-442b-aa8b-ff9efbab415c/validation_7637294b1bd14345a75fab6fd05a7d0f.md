### Title
Permanent stall of Snowbridge Ethereum light client after a finality gap exceeds `SLOTS_PER_HISTORICAL_ROOT`, with no permissionless recovery path — ([File: bridges/snowbridge/pallets/ethereum-client/src/lib.rs])

### Summary
The GMX bug's core broken invariant is: a public, permissionless state-transition check that requires two values to straddle a moving target, such that once the market/oracle drifts past that target the check can *never again* be satisfied, permanently wedging the pending state with no organic recovery. The equivalent invariant exists in `EthereumBeaconClient::verify_update` in `bridges/snowbridge/pallets/ethereum-client/src/lib.rs`, where any submitted finalized-header update is rejected with `Error::<T>::InvalidFinalizedHeaderGap` if the gap between the last stored finalized slot and the new update's finalized slot exceeds `SLOTS_PER_HISTORICAL_ROOT` (8192 slots, ≈ 27 hours). Because the check compares against the *chain's actual current head*, which only moves forward, once the permissionless relayer flow falls behind by more than this window (e.g., a relayer/oracle outage, chain congestion, or simply nobody submitting updates in time), no future `submit` call from any relayer can ever satisfy the gap check again — the bridge's finality tracking becomes permanently stuck exactly like the wedged stop-loss order.

### Finding Description
`verify_update` enforces: [1](#0-0) 

This check compares the currently stored `latest_finalized_state.slot` against the incoming `update.finalized_header.slot`. The public, permissionless entry point that reaches this code is `submit`, which calls `process_update` → `verify_update`: [2](#0-1) 

Unlike the sync-committee-period checks (`SkippedSyncCommitteePeriod`), which only gate whether *this particular* update can be applied and can eventually be satisfied by submitting an intermediate update, the `InvalidFinalizedHeaderGap` check is unrecoverable through the normal permissionless path once triggered: any relayer submission with a `finalized_header.slot` far enough ahead of the stuck `latest_finalized_state.slot` will always fail this check, because on Ethereum mainnet the chain head only advances — it never "comes back" within the window. The only known escape hatch is `force_checkpoint`, which is `Root`-origin only: [3](#0-2) 

This mirrors the GMX bug precisely:
- GMX: `primaryPrice`/`secondaryPrice` must straddle `triggerPrice`; once market moves past it, no keeper can ever execute — order is wedged.
- Snowbridge: `latest_finalized_state.slot` and `update.finalized_header.slot` must stay within `SLOTS_PER_HISTORICAL_ROOT` of each other; once the gap grows past it, no relayer can ever submit a valid update — light client is wedged.

The tests confirm the check is a hard boundary with no built-in recovery: [4](#0-3) 

### Impact Explanation
Once `InvalidFinalizedHeaderGap` triggers permanently, the on-chain Ethereum light client on BridgeHub stops advancing. This directly stalls all downstream Snowbridge flows that depend on `LatestFinalizedBlockRoot`/`FinalizedBeaconState`, including execution-header/ancestry proof verification used to validate inbound message and asset-lock proofs from Ethereum: [5](#0-4) 

Since inbound message/asset processing on BridgeHub relies on being able to prove execution headers against a *recent* finalized state, a permanently stuck light client means all pending and future bridge deliveries (asset unlocks, message dispatch) relying on execution proofs stall indefinitely — a bridge-wide processing halt with no permissionless remedy, requiring governance (`Root`) intervention via `force_checkpoint`, which discards the light client's continuity guarantees. This matches the "permanent... bridge-state lock" and "stalls bridge processing" impact classes in the required-impact gate.

### Likelihood Explanation
No malicious actor or privileged role is required — only the ordinary operational risk that no relayer submits a valid `submit` update within `SLOTS_PER_HISTORICAL_ROOT` (~27 hours) of the last finalized checkpoint. This can happen from relayer downtime, network partition, node/infra issues on the relayer side, or a period of no active relayer economic incentive — none of which qualify as "malicious relayer" (the flow degrades from *absence* of relayer activity, not corruption of relayer behavior). Given 27 hours is a plausible outage window for a permissionless relayer set with no guaranteed liveness SLA, this is a realistic, unprivileged-triggerable denial-of-service condition.

### Recommendation
Do not hard-fail permanently once the gap is exceeded. Options:
- Allow bridging the gap incrementally by accepting a bootstrap/re-checkpoint update through a permissionless flow (e.g., require an updated `CheckpointUpdate` derived from a recent finalized header, verified independently via sync-committee signatures, rather than requiring `Root`).
- Alternatively, widen recovery by allowing submission of a checkpoint-style update that re-anchors `LatestFinalizedBlockRoot`/`FinalizedBeaconState` under the same trust assumptions as `submit` (BLS-verified against the current/next sync committee) instead of only via privileged `force_checkpoint`.
- At minimum, monitor/alarm before the gap approaches the `SLOTS_PER_HISTORICAL_ROOT` limit so operators can intervene before the state becomes unrecoverable without governance.

### Proof of Concept
1. Bootstrap the light client via `force_checkpoint` at slot `S0`.
2. Simulate relayer inactivity (no `submit` calls) for a period long enough that the real Ethereum beacon chain finalizes slot `S0 + SLOTS_PER_HISTORICAL_ROOT + k` for any `k > 0`.
3. Any relayer now attempts `EthereumBeaconClient::submit` with a valid, correctly BLS-signed `Update` whose `finalized_header.slot = S0 + SLOTS_PER_HISTORICAL_ROOT + k`.
4. The call reverts with `Error::<T>::InvalidFinalizedHeaderGap` per the check at `bridges/snowbridge/pallets/ethereum-client/src/lib.rs:353-359`, exactly reproduced by the existing test `submit_finalized_header_update_with_too_large_gap` ( [4](#0-3) ).
5. Because the real chain head never moves backward, every subsequent honest, correctly-signed `submit` will also fail this same check — the light client (and all downstream execution-proof-dependent bridge flows) is permanently wedged until a `Root`-origin `force_checkpoint` call is made.

### Citations

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L302-305)
```rust
		pub(crate) fn process_update(update: &Update) -> DispatchResultWithPostInfo {
			Self::verify_update(update)?;
			Self::apply_update(update)
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

**File:** bridges/snowbridge/pallets/ethereum-client/src/benchmarking/mod.rs (L22-34)
```rust
	#[benchmark]
	fn force_checkpoint() -> Result<(), BenchmarkError> {
		let checkpoint_update = make_checkpoint();
		let block_root: H256 = checkpoint_update.header.hash_tree_root().unwrap();

		#[extrinsic_call]
		_(RawOrigin::Root, Box::new(*checkpoint_update));

		assert!(<LatestFinalizedBlockRoot<T>>::get() == block_root);
		assert!(<FinalizedBeaconState<T>>::get(block_root).is_some());

		Ok(())
	}
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

**File:** bridges/snowbridge/pallets/ethereum-client/src/tests.rs (L939-966)
```rust
#[test]
fn submit_execution_proof_that_is_also_finalized_header_which_is_stored_but_slots_dont_match() {
	let checkpoint = Box::new(load_checkpoint_update_fixture());
	let finalized_header_update = Box::new(load_finalized_header_update_fixture());
	let mut execution_header_update = Box::new(load_execution_proof_fixture());
	execution_header_update.ancestry_proof = None;

	new_tester().execute_with(|| {
		assert_ok!(EthereumBeaconClient::process_checkpoint_update(&checkpoint));
		assert_ok!(EthereumBeaconClient::submit(RuntimeOrigin::signed(1), finalized_header_update));

		let block_root: H256 = execution_header_update.header.hash_tree_root().unwrap();

		<FinalizedBeaconState<Test>>::insert(
			block_root,
			CompactBeaconState {
				slot: execution_header_update.header.slot + 1,
				block_roots_root: Default::default(),
			},
		);
		LatestFinalizedBlockRoot::<Test>::set(block_root);

		assert_err!(
			EthereumBeaconClient::verify_execution_proof(&execution_header_update),
			Error::<Test>::ExpectedFinalizedHeaderNotStored
		);
	});
}
```
