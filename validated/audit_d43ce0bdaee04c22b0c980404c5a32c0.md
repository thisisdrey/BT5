## Finding: Non-fork-aware execution-header Merkle proof index in the Snowbridge Ethereum light client

The repository already contains direct evidence of the exact bug class described in the report: a Merkle-proof generalized-index/tree-depth value assumed constant across an Ethereum consensus fork, while sibling proof paths in the very same pallet were fixed to be fork-aware for the same problem.

### Title
Non-fork-aware execution-header Merkle proof gindex in `EthereumBeaconClient::verify_execution_proof` - (File: `bridges/snowbridge/pallets/ethereum-client/src/lib.rs`)

### Summary
`EthereumBeaconClient::execution_header_gindex()` unconditionally returns the constant `config::altair::EXECUTION_HEADER_INDEX` (=25), independent of the beacon slot/fork, and is consumed by `verify_execution_proof` in `impls.rs` to check that an `execution_payload` is included in a `BeaconBlockBody`. [1](#0-0) [2](#0-1)  In contrast, every other Merkle-branch gindex used by this same pallet (`finalized_root`, `block_roots`, `current_sync_committee`, `next_sync_committee`) is computed via a slot/epoch-gated helper that switches to a distinct `config::electra::*` constant once the epoch crosses the Electra fork boundary, because the `BeaconState` container's field layout/depth changed at Electra. [3](#0-2) 

### Finding Description
`config::altair::EXECUTION_HEADER_INDEX` is only declared once, in the `altair` config module, with no `electra` (or later fork) counterpart, unlike `BLOCK_ROOTS_INDEX`, `FINALIZED_ROOT_INDEX`, `CURRENT_SYNC_COMMITTEE_INDEX`, and `NEXT_SYNC_COMMITTEE_INDEX`, which are all redefined in `config::electra`. [4](#0-3) [5](#0-4) 

`verify_execution_proof` calls `Self::execution_header_gindex()` with no slot/fork argument and feeds the result straight into `verify_merkle_branch` to validate that `execution_payload` is rooted in the beacon block body: [6](#0-5) 

This mirrors exactly the reported bug pattern: a fixed generalized index/tree-depth constant used to verify inclusion of an SSZ container field, where the surrounding container's field count (and hence Merkle depth/index) can change across a hard fork. The pallet's own code demonstrates that the authors are aware such fork-induced index shifts occur — they explicitly added `config::electra::*` variants and slot-gated selector functions (`finalized_root_gindex_at_slot`, `block_roots_gindex_at_slot`, `current_sync_committee_gindex_at_slot`, `next_sync_committee_gindex_at_slot`) for the `BeaconState` container fields impacted by the Electra restructuring, but never extended the same treatment to `execution_header_gindex()` for the `BeaconBlockBody` container, which is exercised on the `submit` extrinsic — an unauthenticated, public, `ensure_signed` entry point reachable by any signed account. [7](#0-6) 

`verify_merkle_branch` does enforce `branch.len() == depth` before accepting a proof, which prevents a same-shaped-but-wrong-depth forgery from an attacker who cannot control the depth argument. [8](#0-7)  However, this guard only protects against attacker-supplied depth mismatches — it does nothing if the pallet itself computes a stale, hard-coded depth/index for the current fork. Should any future hard fork change the field layout of `BeaconBlockBody` (e.g. inserting or removing fields before `execution_payload`, or growing the total field count past a power-of-two boundary that increases the Merkle depth from 4 to 5, exactly as Deneb's two new `ExecutionPayload` fields did to EigenPod), `execution_header_gindex()` will keep returning the old constant `25` unconditionally, since there is no fork-selection logic at all for this specific gindex — unlike the parallel `*_gindex_at_slot` functions that already anticipate this exact class of change for other `BeaconState` fields.

### Impact Explanation
If a hard fork changes the merkleization of `BeaconBlockBody` around the `execution_payload` field (adding/removing/reordering fields that shift its generalized index or the tree depth), `verify_execution_proof` will reject all valid post-fork execution-header inclusion proofs — because the honestly-constructed branch length/index from real chain data will no longer match the stale constant, failing the `branch.len() != depth` check inside `verify_merkle_branch`. Since `verify_execution_proof` gates `Verifier::verify`, which is used by both `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt`, [9](#0-8)  this stalls all Ethereum→Substrate message delivery and delivery-receipt processing (bridge relayer rewards, inbound token/XCM messages) — a full processing halt of the bridge's inbound and receipt-confirmation flow until a pallet code upgrade ships a corrected constant, matching the "permanent... bridge-state lock" / "stalls bridge processing" impact category.

### Likelihood Explanation
The likelihood is tied entirely to the Ethereum consensus-layer roadmap, not to any local privileged actor, so it satisfies the "no malicious peer/validator/relayer/admin required" constraint — any future consensus hard fork that reshapes `BeaconBlockBody`'s field layout (the exact same fork-driven container-growth trigger that caused the real-world EigenPod Deneb bug) will silently desynchronize `execution_header_gindex()` from the real chain data, with no code path currently able to detect or adapt to it. The repository's own precedent — three prior indices already requiring an Electra-specific override — shows this is a recurring, expected event class in this codebase, not a hypothetical one.

### Recommendation
Make `execution_header_gindex()` fork/slot-aware in the same style as `finalized_root_gindex_at_slot`/`block_roots_gindex_at_slot`: accept the relevant slot and `ForkVersions`, compute the epoch, and select among per-fork `EXECUTION_HEADER_INDEX` constants (adding an `electra`/future-fork override module analogous to `config::electra`) rather than hard-coding the Altair-era value. This keeps the execution-header inclusion proof correct if/when a future fork changes `BeaconBlockBody`'s field composition around `execution_payload`, consistent with how the pallet already treats every other consensus-object gindex.

### Proof of Concept
1. Deploy/upgrade the runtime so `T::ForkVersions` includes a hypothetical future fork (e.g. `fulu`, already modeled in `ForkVersions`) whose activation adds/removes a field in `BeaconBlockBody` such that `execution_payload`'s real generalized index or tree depth differs from `config::altair::EXECUTION_HEADER_INDEX` (25, depth 4) — mirroring how Deneb's two new `ExecutionPayload` fields shifted EigenPod's tree from depth 4 to 5.
2. After the fork activates on the source Ethereum chain, submit a legitimately constructed `ExecutionProof` (correct real branch/root per the new fork layout) via `inbound_queue_v2::submit` (or `outbound_queue_v2::submit_delivery_receipt`).
3. `verify_execution_proof` calls `Self::execution_header_gindex()` — still returning the stale constant 25/depth 4 — and passes it into `verify_merkle_branch`, which rejects the proof at the `branch.len() != depth` check because the honestly-generated branch has the fork-correct depth, not the stale one. [6](#0-5) [10](#0-9) 
4. Every legitimate inbound message/delivery receipt after the fork is permanently rejected until a pallet upgrade patches the constant, demonstrating the stall.

### Citations

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L210-224)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight({
			match update.next_sync_committee_update {
				None => T::WeightInfo::submit(),
				Some(_) => T::WeightInfo::submit_with_sync_committee(),
			}
		})]
		#[transactional]
		/// Submits a new finalized beacon header update. The update may contain the next
		/// sync committee.
		pub fn submit(origin: OriginFor<T>, update: Box<Update>) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			ensure!(!Self::operating_mode().is_halted(), Error::<T>::Halted);
			Self::process_update(&update)
		}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L720-765)
```rust
		pub fn finalized_root_gindex_at_slot(slot: u64, fork_versions: ForkVersions) -> usize {
			let epoch = compute_epoch(slot, config::SLOTS_PER_EPOCH as u64);

			if epoch >= fork_versions.electra.epoch {
				return config::electra::FINALIZED_ROOT_INDEX;
			}

			config::altair::FINALIZED_ROOT_INDEX
		}

		pub fn current_sync_committee_gindex_at_slot(
			slot: u64,
			fork_versions: ForkVersions,
		) -> usize {
			let epoch = compute_epoch(slot, config::SLOTS_PER_EPOCH as u64);

			if epoch >= fork_versions.electra.epoch {
				return config::electra::CURRENT_SYNC_COMMITTEE_INDEX;
			}

			config::altair::CURRENT_SYNC_COMMITTEE_INDEX
		}

		pub fn next_sync_committee_gindex_at_slot(slot: u64, fork_versions: ForkVersions) -> usize {
			let epoch = compute_epoch(slot, config::SLOTS_PER_EPOCH as u64);

			if epoch >= fork_versions.electra.epoch {
				return config::electra::NEXT_SYNC_COMMITTEE_INDEX;
			}

			config::altair::NEXT_SYNC_COMMITTEE_INDEX
		}

		pub fn block_roots_gindex_at_slot(slot: u64, fork_versions: ForkVersions) -> usize {
			let epoch = compute_epoch(slot, config::SLOTS_PER_EPOCH as u64);

			if epoch >= fork_versions.electra.epoch {
				return config::electra::BLOCK_ROOTS_INDEX;
			}

			config::altair::BLOCK_ROOTS_INDEX
		}

		pub fn execution_header_gindex() -> usize {
			config::altair::EXECUTION_HEADER_INDEX
		}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-41)
```rust
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
			.map_err(|e| InvalidExecutionProof(e.into()))?;

		Self::verify_receipt_inclusion(
			proof.execution_proof.execution_header.receipts_root(),
			event_log.tx_index,
			&proof.receipt_proof,
			event_log,
		)?;

		Ok(())
	}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L123-139)
```rust
		let execution_header_root: H256 = execution_proof
			.execution_header
			.hash_tree_root()
			.map_err(|_| Error::<T>::BlockBodyHashTreeRootFailed)?;

		let execution_header_gindex = Self::execution_header_gindex();
		ensure!(
			verify_merkle_branch(
				execution_header_root,
				&execution_proof.execution_branch,
				subtree_index(execution_header_gindex),
				generalized_index_length(execution_header_gindex),
				execution_proof.header.body_root
			),
			Error::<T>::InvalidExecutionHeaderProof
		);
		Ok(())
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/config/altair.rs (L1-17)
```rust
// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2023 Snowfork <hello@snowfork.com>

/// Generalized Indices
/// related to Merkle proofs
/// get_generalized_index(BeaconState, 'block_roots')
pub const BLOCK_ROOTS_INDEX: usize = 37;
/// get_generalized_index(BeaconState, 'finalized_checkpoint', 'root')
pub const FINALIZED_ROOT_INDEX: usize = 105;
/// get_generalized_index(BeaconState, 'current_sync_committee')
pub const CURRENT_SYNC_COMMITTEE_INDEX: usize = 54;
/// get_generalized_index(BeaconState, 'next_sync_committee')
pub const NEXT_SYNC_COMMITTEE_INDEX: usize = 55;
///  get_generalized_index(BeaconBlockBody, 'execution_payload')
pub const EXECUTION_HEADER_INDEX: usize = 25;


```

**File:** bridges/snowbridge/pallets/ethereum-client/src/config/electra.rs (L1-13)
```rust
// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2023 Snowfork <hello@snowfork.com>

/// Generalized Indices
/// related to Merkle proofs
/// get_generalized_index(BeaconState, 'block_roots')
pub const BLOCK_ROOTS_INDEX: usize = 69;
/// get_generalized_index(BeaconState, 'finalized_checkpoint', 'root')
pub const FINALIZED_ROOT_INDEX: usize = 169;
/// get_generalized_index(BeaconState, 'current_sync_committee')
pub const CURRENT_SYNC_COMMITTEE_INDEX: usize = 86;
/// get_generalized_index(BeaconState, 'next_sync_committee')
pub const NEXT_SYNC_COMMITTEE_INDEX: usize = 87;
```

**File:** bridges/snowbridge/primitives/beacon/src/merkle_proof.rs (L8-21)
```rust
pub fn verify_merkle_branch(
	leaf: H256,
	branch: &[H256],
	index: usize,
	depth: usize,
	root: H256,
) -> bool {
	// verify the proof length
	if branch.len() != depth {
		return false;
	}
	// verify the computed merkle root
	root == compute_merkle_root(leaf, branch, index)
}
```
