This confirms the vulnerability: `verify_leaves_proof` at `substrate/frame/merkle-mountain-range/src/mmr/mmr.rs:36-64` only checks that the supplied `leaf` hashes to a position consistent with the *attacker-supplied* `proof.leaf_indices` under the given `root` — it never binds `leaf_indices` to any externally-expected block/leaf number. That verification result flows unchanged into `bridges/modules/beefy/src/utils.rs` `verify_beefy_mmr_leaf`, and the extrinsic `submit_commitment` never checks that the leaf's position (or its embedded `parent_number_and_hash`) actually corresponds to `commitment.commitment.block_number`.

### Title
Unbound MMR leaf position allows mis-bound `parent_number_and_hash` to be recorded for an unrelated block number - (File: `bridges/modules/beefy/src/lib.rs`)

### Summary
`Pallet::submit_commitment` in `bridges/modules/beefy/src/lib.rs` verifies a signed BEEFY `commitment` (which carries `commitment.commitment.block_number` and an MMR root) and independently verifies that a supplied `mmr_leaf`/`mmr_proof` pair is included in that same MMR root via `utils::verify_beefy_mmr_leaf`. Nothing ties the *position* of the proven leaf (`mmr_proof.leaf_indices`) to the *block number* claimed by the commitment. The pallet then stores `mmr_leaf.parent_number_and_hash` under key `commitment.commitment.block_number` in `ImportedCommitments`, effectively trusting the caller's claim that "this leaf belongs to this block" without checking it. This mirrors the SP1Blobstream bug: block-height values (`block_number`) are accepted as caller-supplied inputs and blindly combined with a proof of a *different* structure, instead of being derived from/validated against the actual proven data.

### Finding Description
`submit_commitment` (`bridges/modules/beefy/src/lib.rs:200-273`):
```rust
let mmr_root = utils::verify_commitment::<T, I>(&commitment, &current_authority_set_info, &validator_set)?;
utils::verify_beefy_mmr_leaf::<T, I>(&mmr_leaf, mmr_proof, mmr_root)?;
...
ImportedCommitments::<T, I>::insert(
    commitment.commitment.block_number,
    ImportedCommitment::<T, I> { parent_number_and_hash: mmr_leaf.parent_number_and_hash, mmr_root },
);
``` [1](#0-0) 

`verify_commitment` only checks signatures/validator-set-id and extracts the MMR root from the commitment payload; it never touches the leaf. [2](#0-1) 

`verify_beefy_mmr_leaf` only proves that `mmr_leaf` is included in `mmr_root` at whatever position `mmr_proof.leaf_indices` specifies — it does not check that this leaf index corresponds to `commitment.commitment.block_number - 1` (the expected leaf for that block, per the convention noted in the test helper `mock_chain.rs`: "genesis has no leaf => leaf index is header number minus 1"). [3](#0-2) 

Underlying MMR verification (`verify_leaves_proof`) is purely structural: it maps `proof.leaf_indices` to tree positions and checks the merkle path against `root`, with no external binding to any independently-derived expected index. [4](#0-3) 

Because MMR roots are cumulative (every historical leaf remains provable against any later root), an attacker who possesses a *genuinely valid* signed commitment for block `N` (with correct MMR root `R_N`) can pair it with any older leaf `L_M` (`M < N`) together with its valid inclusion proof against `R_N` — this proof will verify successfully since `L_M` really is included in `R_N`. The pallet then writes `ImportedCommitments[N] = { parent_number_and_hash: L_M.parent_number_and_hash, mmr_root: R_N }`, i.e., block `N` in storage is bound to the parent hash/number of an unrelated, arbitrary earlier block `M`.

### Impact Explanation
`ImportedCommitments` is the on-chain light client state used by downstream pallets to validate header-based proofs (storage proofs, transaction/message inclusion proofs) for the bridged chain, as stated in the pallet doc comment ("Given the header hash, other pallets are able to verify header-based proofs"). Poisoning the `parent_number_and_hash` recorded for a given block number lets an attacker mis-bind the light-client's notion of "block N's parent hash" to an unrelated block's hash, which downstream consumers (e.g., relay/message pallets built atop this BEEFY light client) would use to validate ancestry or header proofs — a forged/mis-bound proof-acceptance condition affecting bridge state integrity, matching the "forged or mis-bound proof or state acceptance" impact category.

### Likelihood Explanation
`submit_commitment` is a public, unsigned-origin-independent extrinsic requiring only `ensure_signed` (any account can call it) — no privileged actor, relayer trust, or malicious validator/collator is needed. The only requirement is possession of one legitimately signed BEEFY commitment for a new best block (observable from the network) and any older leaf+proof pair from the same chain's MMR — both are public data. This is a straightforward unprivileged, publicly triggerable attack path with no need for a malicious peer/validator.

### Recommendation
Bind the proven leaf's position to the claimed `commitment.commitment.block_number` before accepting it: derive the expected leaf index from `commitment.commitment.block_number` (mirroring the `block_num_to_leaf_count`/`leaf index = block_number - 1` convention already used in `pallet_beefy_mmr`'s `is_non_canonical`) and assert `mmr_proof.leaf_indices == [expected_index]` (and `mmr_proof.leaf_count` consistent with `block_number`) inside `verify_beefy_mmr_leaf` or `submit_commitment`, rejecting the call if they don't match — analogous to deriving `trusted_block_height`/`target_block_height` directly from the proven light blocks rather than trusting separately supplied values.

### Proof of Concept
1. Wait for/observe a legitimate BEEFY commitment for block `N` (correctly signed, MMR root `R_N`); this is public gossip data.
2. From the historical MMR (also public/reconstructable), obtain any earlier leaf `L_M` (`M < N-1`) and generate a valid MMR inclusion proof of `L_M` against `R_N` (valid because `R_N` accumulates all past leaves).
3. Call `submit_commitment(origin, commitment_for_N, validator_set, Box::new(L_M), proof_of_L_M_against_R_N)` from any signed account.
4. `verify_commitment` succeeds (signatures/validator set/root extraction are all about block `N`'s commitment, untouched by the leaf substitution). `verify_beefy_mmr_leaf` succeeds because `L_M` is genuinely included in `R_N`.
5. `ImportedCommitments::<T, I>::get(N)` now returns `parent_number_and_hash = L_M.parent_number_and_hash` — the parent hash of block `M`, not of block `N-1` — corrupting the light client's record for block `N`.

### Citations

**File:** bridges/modules/beefy/src/lib.rs (L223-248)
```rust
			// Verify commitment and mmr leaf.
			let current_authority_set_info = CurrentAuthoritySetInfo::<T, I>::get();
			let mmr_root = utils::verify_commitment::<T, I>(
				&commitment,
				&current_authority_set_info,
				&validator_set,
			)?;
			utils::verify_beefy_mmr_leaf::<T, I>(&mmr_leaf, mmr_proof, mmr_root)?;

			// Update request count.
			RequestCount::<T, I>::mutate(|count| *count += 1);
			// Update authority set if needed.
			if mmr_leaf.beefy_next_authority_set.id > current_authority_set_info.id {
				CurrentAuthoritySetInfo::<T, I>::put(mmr_leaf.beefy_next_authority_set);
			}

			// Import commitment.
			let block_number_index = commitments_info.next_block_number_index;
			let to_prune = ImportedBlockNumbers::<T, I>::try_get(block_number_index);
			ImportedCommitments::<T, I>::insert(
				commitment.commitment.block_number,
				ImportedCommitment::<T, I> {
					parent_number_and_hash: mmr_leaf.parent_number_and_hash,
					mmr_root,
				},
			);
```

**File:** bridges/modules/beefy/src/utils.rs (L107-126)
```rust
pub(crate) fn verify_commitment<T: Config<I>, I: 'static>(
	commitment: &BridgedBeefySignedCommitment<T, I>,
	authority_set_info: &BridgedBeefyAuthoritySetInfo<T, I>,
	authority_set: &BridgedBeefyAuthoritySet<T, I>,
) -> Result<BridgedMmrHash<T, I>, Error<T, I>> {
	// Ensure that the commitment is signed by the best known BEEFY validator set.
	ensure!(
		commitment.commitment.validator_set_id == authority_set_info.id,
		Error::<T, I>::InvalidCommitmentValidatorSetId
	);
	ensure!(
		commitment.signatures.len() == authority_set_info.len as usize,
		Error::<T, I>::InvalidCommitmentSignaturesLen
	);

	verify_authority_set(authority_set_info, authority_set)?;
	verify_signatures(commitment, authority_set)?;

	extract_mmr_root(commitment)
}
```

**File:** bridges/modules/beefy/src/utils.rs (L128-157)
```rust
/// Verify MMR proof of given leaf.
pub(crate) fn verify_beefy_mmr_leaf<T: Config<I>, I: 'static>(
	mmr_leaf: &BridgedBeefyMmrLeaf<T, I>,
	mmr_proof: BridgedMmrProof<T, I>,
	mmr_root: BridgedMmrHash<T, I>,
) -> Result<(), Error<T, I>> {
	let mmr_proof_leaf_count = mmr_proof.leaf_count;
	let mmr_proof_length = mmr_proof.items.len();

	// Verify the mmr proof for the provided leaf.
	let mmr_leaf_hash = BridgedMmrHashing::<T, I>::hash(&mmr_leaf.encode());
	verify_mmr_leaves_proof(
		mmr_root,
		vec![BridgedMmrDataOrHash::<T, I>::Hash(mmr_leaf_hash)],
		mmr_proof,
	)
	.map_err(|e| {
		tracing::error!(
			target: LOG_TARGET,
			error=?e,
			?mmr_leaf_hash,
			root=?mmr_root,
			leaf_count=%mmr_proof_leaf_count,
			len=%mmr_proof_length,
			"MMR proof of leaf verification has failed"
		);

		Error::<T, I>::MmrProofVerificationFailed
	})
}
```

**File:** substrate/frame/merkle-mountain-range/src/mmr/mmr.rs (L36-64)
```rust
pub fn verify_leaves_proof<H, L>(
	root: H::Output,
	leaves: Vec<Node<H, L>>,
	proof: LeafProof<H::Output>,
) -> Result<bool, Error>
where
	H: Hash,
	L: FullLeaf,
{
	let size = NodesUtils::new(proof.leaf_count).size();

	if leaves.len() != proof.leaf_indices.len() {
		return Err(Error::Verify.log_debug("Proof leaf_indices not same length with leaves"));
	}

	let leaves_and_position_data = proof
		.leaf_indices
		.into_iter()
		.map(|index| mmr_lib::leaf_index_to_pos(index))
		.zip(leaves.into_iter())
		.collect();

	let p = mmr_lib::MerkleProof::<Node<H, L>, Hasher<H, L>>::new(
		size,
		proof.items.into_iter().map(Node::Hash).collect(),
	);
	p.verify(Node::Hash(root), leaves_and_position_data)
		.map_err(|e| Error::Verify.log_debug(e))
}
```
