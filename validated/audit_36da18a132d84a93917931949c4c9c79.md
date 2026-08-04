## Title
Unchecked `prev_leaf_count` in MMR `AncestryProof` causes underflow panic in `verify_ancestry_proof` - ([File: substrate/frame/merkle-mountain-range/src/mmr/mmr.rs])

### Summary
`Pallet::verify_ancestry_proof` (a public, stateless runtime API used by any consumer chain / bridge that validates MMR ancestry proofs, e.g. BEEFY/MMR light clients) forwards an attacker/relayer-supplied `AncestryProof` struct directly to `mmr::verify_ancestry_proof`, which computes `ancestry_proof.prev_leaf_count - 1` without any prior validation that `prev_leaf_count > 0`. This mirrors the reported Gearbox `UniswapV3._extractTokens()` bug class: an externally supplied length/count field is subtracted from without a bounds check, causing underflow.

### Finding Description
`AncestryProof<Hash>` is a plain, unauthenticated-until-verified data structure containing `prev_peaks`, `prev_leaf_count`, `leaf_count`, and `items` — it is decoded/constructed from data supplied by whoever calls `verify_ancestry_proof` (relayers, off-chain workers, or other pallets/bridges consuming MMR proofs). [1](#0-0) 

The verification path is: [2](#0-1) 

Specifically, line 98 computes:
```rust
prev_mmr_size: mmr_lib::helper::leaf_index_to_mmr_size(ancestry_proof.prev_leaf_count - 1),
```
`prev_leaf_count` is a `LeafIndex` (`u64`) taken as-is from the caller-supplied `AncestryProof`. There is **no check** that `prev_leaf_count != 0` before the subtraction, unlike the sibling function `verify_leaves` a few lines above in `lib.rs`, which explicitly guards `proof.leaf_count == 0` before using it: [3](#0-2) 

That symmetrical zero-check is conspicuously absent in `verify_ancestry_proof`. If `prev_leaf_count` is `0`, the subtraction `0u64 - 1` underflows. In a build with overflow checks enabled (the default for Substrate runtimes, since `overflow-checks = true` is set in workspace profiles) this triggers a **panic**, which aborts the current execution context (a WASM trap during block execution/import if invoked in on-chain logic, or an unrecoverable execution error for any consumer pallet/bridge that calls `verify_ancestry_proof` synchronously as part of message/proof processing). In a build without overflow checks, it silently wraps to `u64::MAX`, producing a bogus `prev_mmr_size` passed into `mmr_lib::helper::leaf_index_to_mmr_size`, corrupting the effective ancestry-proof size and root computation used for chain/state binding.

`is_ancestry_proof_optimal` (used to bound proof size for weight-charging) similarly does not reject `prev_leaf_count == 0` before deriving `prev_mmr_size` via `NodesUtils::new(ancestry_proof.prev_leaf_count)`, so no earlier gate stops a zero value from reaching `verify_ancestry_proof`. [4](#0-3) 

### Impact Explanation
`verify_ancestry_proof` is a public pallet function intended to be called by any consumer of MMR-based chain-history proofs (e.g., a bridge or parachain validating that one finalized header is an ancestor of another via MMR roots). Feeding it a crafted `AncestryProof{ prev_leaf_count: 0, .. }` either:
- panics/traps the calling execution context (a public-underpriced-work / DoS-class impact — an unprivileged caller can supply a malformed proof and abort verification logic that other chain/bridge logic depends on), or
- if overflow checks are disabled, wraps to `u64::MAX` and proceeds to compute a nonsensical `prev_mmr_size`/root, undermining the "prove-once, bind-exact-nonce/size" invariant that ancestry proofs must satisfy, potentially causing false acceptance downstream.

### Likelihood Explanation
This requires no privileged actor, malicious node, or governance action — any caller/relayer providing an `AncestryProof` with `prev_leaf_count = 0` (a value they fully control since it's not on-chain state, it's proof input) triggers the code path. The condition is trivial to construct.

### Recommendation
Add an explicit guard in `verify_ancestry_proof` (and `is_ancestry_proof_optimal`) that rejects `ancestry_proof.prev_leaf_count == 0` before performing `prev_leaf_count - 1`, mirroring the zero-check already present in `verify_leaves`. Prefer `checked_sub(1)` / `saturating_sub(1)` and return `Error::Verify` on failure instead of relying on implicit runtime overflow-check panics.

### Proof of Concept
```rust
use sp_mmr_primitives::AncestryProof;

let proof = AncestryProof::<H256> {
    prev_peaks: vec![],
    prev_leaf_count: 0, // attacker-controlled, no validation before use
    leaf_count: 5,
    items: vec![],
};

// Panics (overflow-checks on) or silently wraps to u64::MAX (checks off)
let _ = Pallet::<Test>::verify_ancestry_proof(root, proof);
```

I was not able to confirm within available context which specific downstream consumers (parachains/bridges) call `Pallet::verify_ancestry_proof` synchronously as part of consensus-critical execution versus only off-chain/RPC usage; this affects exact severity classification (chain-halting panic vs. isolated caller failure) and should be verified with a full-context Devin session if a stronger blast-radius determination is needed.

### Citations

**File:** substrate/frame/merkle-mountain-range/src/lib.rs (L406-413)
```rust
		if proof.leaf_count > NumberOfLeaves::<T, I>::get() ||
			proof.leaf_count == 0 ||
			proof.items.len().saturating_add(leaves.len()) as u64 > proof.leaf_count
		{
			return Err(
				Error::Verify.log_debug("The proof has incorrect number of leaves or proof items.")
			);
		}
```

**File:** substrate/frame/merkle-mountain-range/src/lib.rs (L452-457)
```rust
	pub fn verify_ancestry_proof(
		root: HashOf<T, I>,
		ancestry_proof: AncestryProof<HashOf<T, I>>,
	) -> Result<HashOf<T, I>, Error> {
		verify_ancestry_proof::<HashingOf<T, I>, LeafOf<T, I>>(root, ancestry_proof)
	}
```

**File:** substrate/frame/merkle-mountain-range/src/mmr/mmr.rs (L66-76)
```rust
pub fn is_ancestry_proof_optimal<H>(ancestry_proof: &AncestryProof<H::Output>) -> bool
where
	H: frame::traits::Hash,
{
	let prev_mmr_size = NodesUtils::new(ancestry_proof.prev_leaf_count).size();
	let mmr_size = NodesUtils::new(ancestry_proof.leaf_count).size();

	let expected_proof_size =
		mmr_lib::ancestry_proof::expected_ancestry_proof_size(prev_mmr_size, mmr_size);
	ancestry_proof.items.len() == expected_proof_size
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
