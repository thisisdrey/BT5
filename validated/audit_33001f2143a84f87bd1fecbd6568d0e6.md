## Analysis

The Gravity bridge bug's core invariant is: **public entry points that trigger unbounded/expensive on-chain work must have costs (gas/weight) bound to the actual work performed, or the chain/bridge can be stalled**. In this repository, the direct analog is in the Substrate bridge BEEFY pallet's `submit_commitment` extrinsic.

### Title
Zero-weight public `submit_commitment` extrinsic allows underpriced signature-verification work to stall block production - (File: `bridges/modules/beefy/src/lib.rs`)

### Summary
`pallet::submit_commitment` is declared with `#[pallet::weight(0)]` [1](#0-0)  yet internally performs O(N) expensive cryptographic verification work proportional to the configured BEEFY authority-set length, via `utils::verify_signatures` [2](#0-1)  and `utils::get_authorities_mmr_root` [3](#0-2) , called from `utils::verify_commitment` [4](#0-3) . The extrinsic is callable by any signed account (`ensure_signed(origin)?`) [5](#0-4) , and while `MaxRequests` bounds only *successful* commitment writes per block [6](#0-5) , it does not bound the number of *failed* or maliciously-crafted `submit_commitment` calls that can be packed into a block, each still paying to `verify_authority_set`/`verify_signatures` before failing.

### Finding Description
This mirrors the Gravity finding's underlying flaw: the on-chain logic assumes the size of the "authority set" or verification workload is implicitly bounded and cheap, without an explicit sanity/weight check tied to that size. Here, `verify_signatures` iterates through the entire authority set until it reaches `signatures_required` valid signatures [7](#0-6) ; if an attacker submits a commitment carrying an authority set at or near the current chain's true (potentially large) validator-set size but with mostly invalid/missing signatures, the loop must scan up to `authority_set.len()` entries doing full ECDSA/BeefyAuthorityId verification calls before returning `NotEnoughCorrectSignatures`. Because the extrinsic's declared weight is `0`, none of this actual computation is charged against the block's weight limit or the submitter's transaction fee — the runtime's weight-accounting and block-building logic believes this call costs nothing, so many such calls can be included in a single block, or dispatched cheaply and repeatedly, consuming real CPU time disproportionate to the accounted weight.

This is the direct local analog of the Gravity `updateValset` issue: absence of an on-chain sanity check tying verification cost/threshold to the actual signer-set size, enabling free/underpriced computation that can stall processing — except here it manifests as a weight-accounting gap rather than a stuck contract.

### Impact Explanation
An attacker (any signed, unprivileged account able to pay the near-zero transaction fee implied by zero weight) can submit repeated `submit_commitment` calls with large, chain-consistent authority-set lengths and garbage signatures. Since weight is `0`, the runtime's block-weight limit does not throttle inclusion of these calls, allowing an attacker to pack a block with calls whose real execution time (bounded only by `MaxRequests`-independent failed calls) exceeds what the weight system accounts for. This degrades block production throughput for chains running this bridge pallet — squarely matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
High: this requires no privileged role, no malicious relayer/validator/collator, and no compromised keys — just a signed account submitting crafted (fee-paying, low-cost) transactions with an invalid signature set matching the chain's known authority-set length, which is public information exposed via `CurrentAuthoritySetInfo`. Every failing/near-passing call still walks the authority-set loop before returning an error.

### Recommendation
Replace `#[pallet::weight(0)]` on `submit_commitment` with a weight function proportional to `validator_set.len()` (e.g., benchmark cost per verified signature and per authority in the MMR-root recomputation), so the weight/fee scales with the real verification cost, consistent with how other calls in the pallet use `T::DbWeight::get().reads_writes(...)`. Additionally, consider capping the maximum accepted `validator_set` length early (fail-fast before the verification loop) to bound worst-case per-call cost.

### Proof of Concept
1. Observe `CurrentAuthoritySetInfo` to learn the current authority-set `len` (public storage).
2. Craft a `BridgedBeefySignedCommitment` with `signatures.len() == authority_set_info.len` but with mostly `None`/invalid entries, keeping the `commitment.validator_set_id` matching, so it passes the early length/id checks in `verify_commitment` [8](#0-7)  and only fails at `verify_signatures`'s missing-signature check after scanning the whole set.
3. Submit many such `submit_commitment` transactions in the same block; because weight is `0`, the runtime's block-weight accounting will not restrict inclusion, letting the attacker force the block author to execute O(N) signature-verification cycles per transaction across many transactions "for free," degrading real block-production throughput.

### Citations

**File:** bridges/modules/beefy/src/lib.rs (L105-112)
```rust
		/// The upper bound on the number of requests allowed by the pallet.
		///
		/// A request refers to an action which writes a header to storage.
		///
		/// Once this bound is reached the pallet will reject all commitments
		/// until the request count has decreased.
		#[pallet::constant]
		type MaxRequests: Get<u32>;
```

**File:** bridges/modules/beefy/src/lib.rs (L198-200)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(0)]
		pub fn submit_commitment(
```

**File:** bridges/modules/beefy/src/lib.rs (L210-211)
```rust
			Self::ensure_not_halted().map_err(Error::<T, I>::BridgeModule)?;
			ensure_signed(origin)?;
```

**File:** bridges/modules/beefy/src/utils.rs (L17-31)
```rust
/// Get the MMR root for a collection of validators.
pub(crate) fn get_authorities_mmr_root<
	'a,
	T: Config<I>,
	I: 'static,
	V: Iterator<Item = &'a BridgedBeefyAuthorityId<T, I>>,
>(
	authorities: V,
) -> BridgedMmrHash<T, I> {
	let merkle_leafs = authorities
		.cloned()
		.map(BridgedBeefyAuthorityIdToMerkleLeaf::<T, I>::convert)
		.collect::<Vec<_>>();
	merkle_root::<BridgedMmrHashing<T, I>, _>(merkle_leafs)
}
```

**File:** bridges/modules/beefy/src/utils.rs (L55-94)
```rust
pub(crate) fn signatures_required(validators_len: usize) -> usize {
	validators_len - validators_len.saturating_sub(1) / 3
}

fn verify_signatures<T: Config<I>, I: 'static>(
	commitment: &BridgedBeefySignedCommitment<T, I>,
	authority_set: &BridgedBeefyAuthoritySet<T, I>,
) -> Result<(), Error<T, I>> {
	ensure!(
		commitment.signatures.len() == authority_set.len(),
		Error::<T, I>::InvalidCommitmentSignaturesLen
	);

	// Ensure that the commitment was signed by enough authorities.
	let msg = commitment.commitment.encode();
	let mut missing_signatures = signatures_required(authority_set.len());
	for (idx, (authority, maybe_sig)) in
		authority_set.validators().iter().zip(commitment.signatures.iter()).enumerate()
	{
		if let Some(sig) = maybe_sig {
			if authority.verify(sig, &msg) {
				missing_signatures = missing_signatures.saturating_sub(1);
				if missing_signatures == 0 {
					break;
				}
			} else {
				tracing::debug!(
					target: LOG_TARGET,
					%idx,
					?authority,
					?sig,
					"Signed commitment contains incorrect signature of validator"
				);
			}
		}
	}
	ensure!(missing_signatures == 0, Error::<T, I>::NotEnoughCorrectSignatures);

	Ok(())
}
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
