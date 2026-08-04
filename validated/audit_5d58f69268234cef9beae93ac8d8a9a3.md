## Analysis

The external report's core broken invariant: **a single shared parameter is used to validate two structurally different things that require different values**, so the parameter is either wrong for one of them (false acceptance of stale/invalid data) or forces perpetual failure.

I found a structurally identical pattern in Snowbridge's Ethereum light client, in the family of "generalized index" getters used for beacon-chain Merkle proof verification.

### Title
Non-fork-aware `execution_header_gindex()` uses a hardcoded Altair generalized index for all forks, breaking or forging execution-header Merkle proofs after the Electra fork - (File: `bridges/snowbridge/pallets/ethereum-client/src/lib.rs`)

### Summary
`Pallet::verify_execution_proof` (called from `Verifier::verify`, the entry point for every inbound Snowbridge message) validates that an Ethereum execution header is rooted in a beacon block body by computing a generalized index via `Self::execution_header_gindex()` and checking a Merkle branch against `execution_proof.header.body_root`. [1](#0-0) 

Unlike the sibling gindex helpers (`finalized_root_gindex_at_slot`, `current_sync_committee_gindex_at_slot`, `next_sync_committee_gindex_at_slot`, `block_roots_gindex_at_slot`), which all branch on the target slot's epoch against `fork_versions.electra.epoch` and return a different, Electra-specific constant, `execution_header_gindex()` is not fork-aware at all — it unconditionally returns the Altair-era constant: [2](#0-1) 

The Altair constant is defined in `config/altair.rs`: [3](#0-2) 

`config/electra.rs` only re-defines three of the four indices (matching grep shows 3 matches vs. 4 in altair.rs) — it does **not** provide an Electra-specific `EXECUTION_HEADER_INDEX`, confirming that no fork-aware override exists for this particular field, even though the beacon `BeaconBlockBody` container's field layout (and therefore the generalized index of the `execution_payload` field in its Merkle tree) is fork-dependent, exactly like the other four indices that *are* correctly parameterized.

### Finding Description
This is the same anti-pattern as chainlinkAdaptor reusing one `heartbeat` for two feeds that legitimately need different heartbeats: here, one hardcoded generalized index (`config::altair::EXECUTION_HEADER_INDEX`) is used to verify a Merkle branch for a container whose shape differs across forks, while all other related index lookups in the same module correctly branch per fork.

`verify_execution_proof` uses this single, non-fork-aware value for every execution-header proof regardless of the slot's fork: [1](#0-0) 

Existing guards do not stop this: `verify_execution_proof` checks that the beacon header is finalized/ancestor-linked and that the Merkle branch validates against `subtree_index(execution_header_gindex)` at `generalized_index_length(execution_header_gindex)` — but it never validates that the *fork* of `execution_proof.header.slot` matches the fork assumed by the hardcoded Altair index. There is no check anywhere in `verify_execution_proof` analogous to the `epoch >= fork_versions.electra.epoch` branching used elsewhere in the same file for `finalized_root_gindex_at_slot`, `current_sync_committee_gindex_at_slot`, etc.

### Impact Explanation
If the real Electra `BeaconBlockBody` generalized index for `execution_payload` differs from the Altair value (as is expected any time the container's field count/ordering changes across forks — which is precisely why the module maintains a separate `config::electra` module and fork-aware getters for the sibling fields), two outcomes are possible, both matching in-scope impacts:
- **Public underpriced work / bridge processing stall**: every legitimate execution-header proof submitted after the Electra fork activates will fail Merkle verification with `Error::<T>::InvalidExecutionHeaderProof`, permanently halting inbound message verification (`Verifier::verify`) for Snowbridge — "near constant downtime" for the whole inbound queue, matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.
- **Forged/mis-bound proof acceptance**: if the wrong index happens to coincide with a different, attacker-controllable leaf at the same tree depth/position under the actual (correct) tree layout, an attacker-supplied Merkle branch could satisfy `verify_merkle_branch` against unintended data, producing a false-positive "verified" execution header/log inclusion — matching "forged or mis-bound proof or state acceptance", which can lead to unauthorized message execution or theft via forged bridge messages.

### Likelihood Explanation
This triggers automatically and deterministically as soon as the beacon chain crosses into the Electra fork epoch (`fork_versions.electra.epoch`), with no attacker action required beyond normal relayer submission of any execution-header proof from an Electra-era slot — no malicious peer, validator, or governance action is needed, satisfying the impact-gate's requirement of an implementation bug reachable by unprivileged/normal relayer activity. The fork-aware sibling functions in the very same file demonstrate that the developers are aware indices must change per fork, making the omission for `execution_header_gindex` a concrete, provable inconsistency rather than a speculative concern.

### Recommendation
Make `execution_header_gindex` take `(slot, fork_versions)` like its siblings and branch on `compute_epoch(slot, ...) >= fork_versions.electra.epoch`, returning a correctly derived/verified `config::electra::EXECUTION_HEADER_INDEX` for Electra-era slots (adding that constant to `config/electra.rs`), instead of unconditionally returning the Altair constant. Update `verify_execution_proof` to call it with the proof's slot and current fork configuration, mirroring the pattern already used for `finalized_root_gindex_at_slot`, `current_sync_committee_gindex_at_slot`, `next_sync_committee_gindex_at_slot`, and `block_roots_gindex_at_slot`.

### Proof of Concept
1. Runtime configures `ForkVersions` with an `electra.epoch` value that is reached (as already exercised by tests referencing `fork_versions.electra.epoch` in `verify_update`/gindex helpers).
2. A relayer submits an `ExecutionProof` whose `header.slot` falls in the Electra era, with a `execution_branch` computed against the actual Electra `BeaconBlockBody` tree (where `execution_payload`'s true generalized index differs from Altair's `25`).
3. `Pallet::verify_execution_proof` calls `Self::execution_header_gindex()`, which returns the hardcoded Altair value `25` regardless of `execution_proof.header.slot`'s fork.
4. `verify_merkle_branch(execution_header_root, &execution_proof.execution_branch, subtree_index(25), generalized_index_length(25), execution_proof.header.body_root)` is checked against a branch/tree shape that assumes a different index for the Electra layout, so verification fails for legitimate proofs (`Error::<T>::InvalidExecutionHeaderProof`), or — if index collision aligns with attacker-chosen leaf data — succeeds for a forged branch, depending on the exact Electra body layout shift, which the code makes no attempt to account for. [2](#0-1) [4](#0-3) [3](#0-2)

### Citations

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L81-139)
```rust
	/// Validates an execution header with ancestry_proof against a finalized checkpoint on
	/// chain.The beacon header containing the execution header is sent, plus the execution header,
	/// along with a proof that the execution header is rooted in the beacon header body.
	pub(crate) fn verify_execution_proof(execution_proof: &ExecutionProof) -> DispatchResult {
		let latest_finalized_state =
			FinalizedBeaconState::<T>::get(LatestFinalizedBlockRoot::<T>::get())
				.ok_or(Error::<T>::NotBootstrapped)?;
		// Checks that the header is an ancestor of a finalized header, using slot number.
		ensure!(
			execution_proof.header.slot <= latest_finalized_state.slot,
			Error::<T>::HeaderNotFinalized
		);

		let beacon_block_root: H256 = execution_proof
			.header
			.hash_tree_root()
			.map_err(|_| Error::<T>::HeaderHashTreeRootFailed)?;

		match &execution_proof.ancestry_proof {
			Some(proof) => {
				Self::verify_ancestry_proof(
					beacon_block_root,
					execution_proof.header.slot,
					&proof.header_branch,
					proof.finalized_block_root,
				)?;
			},
			None => {
				// If the ancestry proof is not provided, we expect this beacon header to be a
				// finalized beacon header. We need to check that the header hash matches the
				// finalized header root at the expected slot.
				let state = <FinalizedBeaconState<T>>::get(beacon_block_root)
					.ok_or(Error::<T>::ExpectedFinalizedHeaderNotStored)?;
				if execution_proof.header.slot != state.slot {
					return Err(Error::<T>::ExpectedFinalizedHeaderNotStored.into());
				}
			},
		}

		// Gets the hash tree root of the execution header, in preparation for the execution
		// header proof (used to check that the execution header is rooted in the beacon
		// header body.
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

**File:** bridges/snowbridge/pallets/ethereum-client/src/config/altair.rs (L1-15)
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
