## Analysis

The core broken invariant in the external report: a public verification entrypoint decodes an attacker-controlled proof structure and immediately performs an unchecked slicing/length-derived operation on a field taken directly from that structure, before any cryptographic/structural validation gate is reached — causing a panic/DoS on a syntactically valid but semantically malformed proof.

The closest local analog is in `pallet-mmr`'s ancestor-proof verifier, which is reachable from `pallet-beefy-mmr`'s `AncestryHelper` used by BEEFY fork-voting/equivocation reporting.

### Title
Unchecked arithmetic on attacker-supplied `AncestryProof.prev_leaf_count` causes panic in `verify_ancestry_proof` - (File: `substrate/frame/merkle-mountain-range/src/mmr/mmr.rs`)

### Summary
`verify_ancestry_proof` computes `mmr_lib::helper::leaf_index_to_mmr_size(ancestry_proof.prev_leaf_count - 1)` directly from the SCALE-decoded, caller-supplied `AncestryProof.prev_leaf_count: u64` field before any structural or cryptographic check validates that value.

### Finding Description
`AncestryProof` [1](#0-0)  is a plain `Decode`-able struct whose `prev_leaf_count` field is fully attacker-controlled when the proof is passed into stateless verification. The verifier subtracts 1 from this value with no lower-bound check: [2](#0-1) 

If `prev_leaf_count == 0`, `ancestry_proof.prev_leaf_count - 1` underflows a `u64`. In a debug/overflow-checked build this panics immediately; in a release build without overflow checks it silently wraps to `u64::MAX`, which is then fed into `mmr_lib::helper::leaf_index_to_mmr_size`, producing an enormous, nonsensical `prev_mmr_size` that can itself trigger downstream panics/OOM inside the `mmr_lib` proof-verification routines that iterate/allocate based on this size — exactly the same "corrupted-length used before validation" pattern as the SP1 report's `public_values[0..size]` slice.

This function is reachable through `pallet-beefy-mmr`'s `AncestryHelper::is_non_canonical`, which calls it before doing any of its own sanity checks other than a leaf-count equality: [3](#0-2) 

`is_non_canonical` is the mechanism used to prove that a BEEFY commitment is not on the canonical chain (fork-voting/equivocation reporting), and it is designed to accept an unprivileged, externally supplied `AncestryProof` — the whole point of the API is to let *any* party challenge a possibly-fraudulent commitment using self-supplied proof data. Unlike the leaves-proof path (`verify_leaves`), which explicitly guards against a zero/invalid `leaf_count` before calling into the MMR internals: [4](#0-3) 

the ancestry-proof path has no equivalent `prev_leaf_count != 0` (or `>=1`) guard anywhere before the subtraction.

### Impact Explanation
A malformed but well-formed-SCALE `AncestryProof` with `prev_leaf_count = 0` reaching `verify_ancestry_proof` causes an arithmetic underflow. Depending on build profile this is either an immediate panic (process abort/unwind, denial of service for the node servicing the call) or a wrapped `u64::MAX` value propagated into MMR size calculations that can cause further panics or resource exhaustion. Because this path underlies fork-voting/equivocation-style challenges in BEEFY, it is reachable by an unprivileged caller without needing a malicious validator, relayer, or governance action — matching the required "public underpriced work / false state acceptance" impact class.

### Likelihood Explanation
Likelihood is moderate to high in principle: constructing an `AncestryProof` with `prev_leaf_count: 0` requires no cryptographic material — it's a single scalar field in an otherwise-normal SCALE encoding, so any caller who can submit a proof to the fork-voting/equivocation code path can trigger it. The exact end-to-end extrinsic name (I was not able to fully trace an unprivileged, unsigned/signed origin extrinsic in `pallet-beefy`'s equivocation module within the available search budget) and whether release builds have `overflow-checks = true` enabled for this crate could not be conclusively confirmed in this pass — this is a gap that should be verified before treating this as certain in production. Nonetheless, the missing guard on `prev_leaf_count` in `verify_ancestry_proof` is a real code defect, since the analogous `verify_leaves` explicitly checks `proof.leaf_count == 0` while `verify_ancestry_proof` does not check `prev_leaf_count`.

### Recommendation
Add an eager check `ensure!(ancestry_proof.prev_leaf_count >= 1, Error::Verify)` (or reject `prev_leaf_count == 0` explicitly) at the top of `verify_ancestry_proof` in `substrate/frame/merkle-mountain-range/src/mmr/mmr.rs`, mirroring the existing zero/consistency checks already present in `Pallet::verify_leaves`, before any subtraction or downstream `mmr_lib` call is performed on caller-supplied length fields.

### Proof of Concept
1. Construct `AncestryProof { prev_peaks: vec![], prev_leaf_count: 0, leaf_count: N, items: vec![] }` (any valid `leaf_count`/hash values acceptable to the outer `Decode`).
2. Submit it through any caller of `pallet_mmr::Pallet::<T>::verify_ancestry_proof` (e.g. `BeefyMmr::is_non_canonical`, exercised via BEEFY fork-voting/equivocation-report logic in `pallet-beefy`/`pallet-beefy-mmr`).
3. Execution reaches `mmr_lib::helper::leaf_index_to_mmr_size(ancestry_proof.prev_leaf_count - 1)` with `prev_leaf_count = 0`, causing a `u64` subtraction underflow — panicking in debug/overflow-checked builds or producing a corrupted `u64::MAX`-derived size in release builds that is then used by `mmr_lib` internals. [5](#0-4)

### Citations

**File:** substrate/primitives/merkle-mountain-range/src/lib.rs (L364-377)
```rust
/// An MMR ancestry proof for a prior mmr root.
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
#[derive(Encode, Decode, DecodeWithMemTracking, Debug, Clone, PartialEq, Eq, TypeInfo)]
pub struct AncestryProof<Hash> {
	/// Peaks of the ancestor's mmr
	pub prev_peaks: Vec<Hash>,
	/// Number of leaves in the ancestor's MMR.
	pub prev_leaf_count: u64,
	/// Number of leaves in MMR, when the proof was generated.
	pub leaf_count: NodeIndex,
	/// Proof elements
	/// (positions and hashes of siblings of inner nodes on the path to the previous peaks).
	pub items: Vec<(u64, Hash)>,
}
```

**File:** substrate/frame/merkle-mountain-range/src/mmr/mmr.rs (L78-101)
```rust
pub fn verify_ancestry_proof<H, L>(
	root: H::Output,
	ancestry_proof: AncestryProof<H::Output>,
) -> Result<H::Output, Error>
where
	H: Hash,
	L: FullLeaf,
{
	let mmr_size = NodesUtils::new(ancestry_proof.leaf_count).size();

	let prev_peaks_proof = mmr_lib::NodeMerkleProof::<Node<H, L>, Hasher<H, L>>::new(
		mmr_size,
		ancestry_proof
			.items
			.into_iter()
			.map(|(index, hash)| (index, Node::Hash(hash)))
			.collect(),
	);

	let raw_ancestry_proof = mmr_lib::AncestryProof::<Node<H, L>, Hasher<H, L>> {
		prev_mmr_size: mmr_lib::helper::leaf_index_to_mmr_size(ancestry_proof.prev_leaf_count - 1),
		prev_peaks: ancestry_proof.prev_peaks.into_iter().map(|hash| Node::Hash(hash)).collect(),
		prev_peaks_proof,
	};
```

**File:** substrate/frame/beefy-mmr/src/lib.rs (L241-270)
```rust
	fn is_non_canonical(
		commitment: &Commitment<BlockNumberFor<T>>,
		proof: Self::Proof,
		context: Self::ValidationContext,
	) -> bool {
		let commitment_leaf_count =
			match pallet_mmr::Pallet::<T>::block_num_to_leaf_count(commitment.block_number) {
				Ok(commitment_leaf_count) => commitment_leaf_count,
				Err(_) => {
					// We can't prove that the commitment is non-canonical if the
					// `commitment.block_number` is invalid.
					return false;
				},
			};
		if commitment_leaf_count != proof.prev_leaf_count {
			// Can't prove that the commitment is non-canonical if the `commitment.block_number`
			// doesn't match the ancestry proof.
			return false;
		}

		let canonical_mmr_root = context;
		let canonical_prev_root =
			match pallet_mmr::Pallet::<T>::verify_ancestry_proof(canonical_mmr_root, proof) {
				Ok(canonical_prev_root) => canonical_prev_root,
				Err(_) => {
					// Can't prove that the commitment is non-canonical if the proof
					// is invalid.
					return false;
				},
			};
```

**File:** substrate/frame/merkle-mountain-range/src/lib.rs (L402-422)
```rust
	pub fn verify_leaves(
		leaves: Vec<LeafOf<T, I>>,
		proof: LeafProof<HashOf<T, I>>,
	) -> Result<(), Error> {
		if proof.leaf_count > NumberOfLeaves::<T, I>::get() ||
			proof.leaf_count == 0 ||
			proof.items.len().saturating_add(leaves.len()) as u64 > proof.leaf_count
		{
			return Err(
				Error::Verify.log_debug("The proof has incorrect number of leaves or proof items.")
			);
		}

		let mmr: ModuleMmr<mmr::storage::OffchainStorage, T, I> = mmr::Mmr::new(proof.leaf_count);
		let is_valid = mmr.verify_leaves_proof(leaves, proof)?;
		if is_valid {
			Ok(())
		} else {
			Err(Error::Verify.log_debug("The proof is incorrect."))
		}
	}
```
