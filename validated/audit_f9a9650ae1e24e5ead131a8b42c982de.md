Audit Report

## Title
Unchecked `prev_leaf_count` in MMR `AncestryProof` causes underflow in `verify_ancestry_proof` - ([File: substrate/frame/merkle-mountain-range/src/mmr/mmr.rs])

## Summary
`mmr::verify_ancestry_proof` computes `ancestry_proof.prev_leaf_count - 1` without first checking `prev_leaf_count != 0`, unlike the sibling function `verify_leaves` which explicitly rejects `proof.leaf_count == 0`. A caller-supplied `AncestryProof` with `prev_leaf_count: 0` reaches this subtraction unguarded.

## Finding Description
`Pallet::verify_ancestry_proof` forwards the caller-supplied `AncestryProof<Hash>` directly to `mmr::verify_ancestry_proof` [1](#0-0) , which performs `mmr_lib::helper::leaf_index_to_mmr_size(ancestry_proof.prev_leaf_count - 1)` with no prior validation of `prev_leaf_count` [2](#0-1) . By contrast, `verify_leaves` explicitly rejects `proof.leaf_count == 0` before using it [3](#0-2) . `is_ancestry_proof_optimal`, which is meant to gate proof size/weight, also does not reject `prev_leaf_count == 0` before deriving `prev_mmr_size` [4](#0-3) , so nothing upstream stops a zero value from reaching the vulnerable subtraction.

I was unable to locate a workspace-level `overflow-checks = true` setting confirming that this would panic by default in a release runtime build within this repository's `Cargo.toml`; the search only found unrelated matches in a fuzzer README and generic `Cargo.toml` references, so I could not conclusively verify whether this underflow panics (Rust debug-mode / overflow-checks-enabled build) or silently wraps to `u64::MAX` (release build without overflow checks) in the actual runtime build configuration used in production. I also could not confirm within available context which specific downstream consumers (parachains/bridges, e.g., Snowbridge Ethereum client) call `Pallet::verify_ancestry_proof` synchronously as part of consensus-critical block execution versus off-chain/RPC-only usage — this materially affects whether the impact is a genuine chain-halting/block-production-stalling issue versus an isolated caller-side proof-rejection failure.

## Impact Explanation
If overflow checks are active in production runtime builds, this triggers a panic/WASM trap when `verify_ancestry_proof` is invoked synchronously inside consensus-critical logic, which could match the "public underpriced work that degrades block production or stalls bridge processing" impact category. If overflow checks are disabled, the value silently wraps to `u64::MAX`, corrupting the derived `prev_mmr_size` used in ancestry-proof/root computation — a mis-bound proof-verification input, though on a value (`u64::MAX`) so large that `mmr_lib::helper::leaf_index_to_mmr_size` and subsequent peak/size computations would almost certainly produce a proof-size mismatch or verification failure rather than a false-positive acceptance, since the actual proof `items` supplied by the caller would not match the resulting (absurd) `prev_mmr_size`. I could not confirm a concrete path to false acceptance of a forged ancestry proof; the more clearly demonstrable effect is a hard failure (panic or verification error) on malformed input, not a state-corruption or fund-theft outcome.

## Likelihood Explanation
Constructing `AncestryProof { prev_leaf_count: 0, .. }` requires no privileged access — the struct is a plain, caller-controlled input to a public/stateless function. However, the exact user-facing severity (crash/DoS of a critical execution path vs. simple rejection of a bad proof by the caller) depends on details not confirmed here (overflow-checks default in the actual runtime build, and whether any consensus-critical pallet calls this synchronously without a wrapping panic-catch or prior sanitation).

## Recommendation
Add an explicit guard in `verify_ancestry_proof` (and `is_ancestry_proof_optimal`) rejecting `ancestry_proof.prev_leaf_count == 0` before the `prev_leaf_count - 1` subtraction, mirroring the zero-check in `verify_leaves`, and use `checked_sub`/`saturating_sub` returning `Error::Verify` on failure instead of relying on implicit overflow-check panic behavior.

## Proof of Concept
```rust
use sp_mmr_primitives::AncestryProof;

let proof = AncestryProof::<H256> {
    prev_peaks: vec![],
    prev_leaf_count: 0, // caller-controlled, unchecked
    leaf_count: 5,
    items: vec![],
};

let _ = Pallet::<Test>::verify_ancestry_proof(root, proof);
// panics under overflow-checks, or wraps to u64::MAX otherwise
```
Because I could not confirm the workspace's `overflow-checks` setting nor the exact downstream consensus-critical call sites of `verify_ancestry_proof` within this repository, the severity classification (chain-halting/bridge-stalling DoS vs. localized caller-side proof rejection) is uncertain and should be validated with a full-context Devin session that can build and run this reproduction against the actual runtime configuration.

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

**File:** substrate/frame/merkle-mountain-range/src/mmr/mmr.rs (L97-98)
```rust
	let raw_ancestry_proof = mmr_lib::AncestryProof::<Node<H, L>, Hasher<H, L>> {
		prev_mmr_size: mmr_lib::helper::leaf_index_to_mmr_size(ancestry_proof.prev_leaf_count - 1),
```
