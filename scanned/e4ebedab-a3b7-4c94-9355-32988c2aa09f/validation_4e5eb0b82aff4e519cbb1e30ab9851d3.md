The strongest local analog is in the BEEFY bridge light-client pallet, where the authority-set update path lacks the same "non-empty" validation that the same code applies at initialization time, producing the exact same defect class as the Validium report: a signature check that is trivially satisfied once the authority set becomes empty.

### Title
Missing non-empty check on updated BEEFY authority set allows zero-signature commitment acceptance - (File: `bridges/modules/beefy/src/lib.rs`)

### Summary
`pallet-bridge-beefy`'s `initialize()` explicitly rejects an authority set with `len == 0` [1](#0-0) , but the authority-set rotation that happens inside the unprivileged, signed `submit_commitment` extrinsic performs no such check before overwriting `CurrentAuthoritySetInfo` [2](#0-1) . Once `CurrentAuthoritySetInfo.len` is `0`, `utils::verify_signatures` degenerates to a vacuously-true check, mirroring the Validium `setupCommittee()` bug where a `requiredAmountOfSignatures = 0` committee lets any batch pass without real signatures.

### Finding Description
`verify_commitment` requires `commitment.signatures.len() == authority_set_info.len as usize` [3](#0-2) . `verify_signatures` computes `missing_signatures = signatures_required(authority_set.len())` and then iterates over `authority_set.validators().iter().zip(commitment.signatures.iter())` [4](#0-3) . If `authority_set.len() == 0`, `signatures_required(0)` returns `0` [5](#0-4) , the zipped iterator is empty, the loop body never executes, and `ensure!(missing_signatures == 0, ...)` passes trivially with zero real signatures checked [6](#0-5) . This is structurally identical to the reported Validium flaw: `requiredAmountOfSignatures = 0` causes `verifySignatures()` to accept an empty `signaturesAndAddrs`.

The corrupted value is `CurrentAuthoritySetInfo.len` (and the associated `id`/`keyset_commitment`), which can be driven to `0` through the authority-set-rotation branch of `submit_commitment` — a path that, unlike `initialize()`, has no `ensure!(len != 0, ...)` guard.

### Impact Explanation
Once the stored authority set's `len` becomes `0`, every subsequent `submit_commitment` call from any signed account can supply an empty `validator_set` and a commitment with zero signatures and have it accepted as long as it also satisfies the (now vacuous for an empty set) MMR root/keyset-commitment check and cryptographic checks that no longer meaningfully constrain anything. This breaks the "forged or mis-bound proof acceptance" pivot for BEEFY-based bridge finality — the pallet is meant to gate imported commitments on real cryptographic finality, and an empty authority set removes that gate entirely, allowing downstream MMR-leaf/storage-proof consumers to trust attacker-influenced data.

### Likelihood Explanation
Reaching `len == 0` in `CurrentAuthoritySetInfo` requires the bridged chain's real BEEFY digest (`mmr_leaf.beefy_next_authority_set`) to actually report an empty next-authority-set with a higher `id`, which is bound cryptographically to a genuinely signed commitment from the bridged chain — this is not directly forgeable by an unprivileged caller of `submit_commitment` alone. However, the pallet code itself provides no defense-in-depth against this state ever being written, even though the exact same class of check exists (and is enforced) for `initialize()`. This is a missing-invariant/local code gap rather than a scenario requiring compromise of this chain's own privileged accounts.

### Recommendation
Add the same guard used in `initialize()` to the authority-set update branch of `submit_commitment`:
```rust
if mmr_leaf.beefy_next_authority_set.id > current_authority_set_info.id {
    ensure!(mmr_leaf.beefy_next_authority_set.len != 0, Error::<T, I>::InvalidValidatorSetLen);
    CurrentAuthoritySetInfo::<T, I>::put(mmr_leaf.beefy_next_authority_set);
}
```
This keeps the pallet's non-empty-authority-set invariant intact across its entire lifecycle, not just at initialization.

### Proof of Concept
1. Deploy/initialize `pallet-bridge-beefy` normally with a non-empty authority set via `initialize()` [7](#0-6) .
2. Submit a validly-signed commitment (from the real, non-empty current authority set) whose MMR leaf declares `beefy_next_authority_set = { id: current_id + 1, len: 0, keyset_commitment: <empty-merkle-root> }`. This is accepted because `submit_commitment` only checks `id > current_authority_set_info.id` before overwriting, with no `len != 0` guard [8](#0-7) .
3. `CurrentAuthoritySetInfo` now has `len = 0`.
4. Any signed account calls `submit_commitment` again with `validator_set = []` and a commitment carrying `signatures = []`. `verify_commitment` passes the length checks (`0 == 0`) [9](#0-8) , `verify_authority_set` passes (empty merkle root matches empty keyset commitment) [10](#0-9) , and `verify_signatures` passes vacuously as shown above [11](#0-10) .
5. The pallet imports the "verified" commitment and its MMR root into storage despite zero real signatures having been checked.

### Citations

**File:** bridges/modules/beefy/src/lib.rs (L153-167)
```rust
		/// Initialize pallet with BEEFY authority set and best known finalized block number.
		#[pallet::call_index(0)]
		#[pallet::weight((T::DbWeight::get().reads_writes(2, 3), DispatchClass::Operational))]
		pub fn initialize(
			origin: OriginFor<T>,
			init_data: InitializationDataOf<T, I>,
		) -> DispatchResult {
			Self::ensure_owner_or_root(origin)?;

			let is_initialized = <ImportedCommitmentsInfo<T, I>>::exists();
			ensure!(!is_initialized, <Error<T, I>>::AlreadyInitialized);

			tracing::info!(target: LOG_TARGET, ?init_data, "Initializing bridge BEEFY pallet");
			Ok(initialize::<T, I>(init_data)?)
		}
```

**File:** bridges/modules/beefy/src/lib.rs (L224-237)
```rust
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
```

**File:** bridges/modules/beefy/src/lib.rs (L388-393)
```rust
	pub(super) fn initialize<T: Config<I>, I: 'static>(
		init_data: InitializationDataOf<T, I>,
	) -> Result<(), Error<T, I>> {
		if init_data.authority_set.len == 0 {
			return Err(Error::<T, I>::InvalidInitialAuthoritySet);
		}
```

**File:** bridges/modules/beefy/src/utils.rs (L33-48)
```rust
fn verify_authority_set<T: Config<I>, I: 'static>(
	authority_set_info: &BridgedBeefyAuthoritySetInfo<T, I>,
	authority_set: &BridgedBeefyAuthoritySet<T, I>,
) -> Result<(), Error<T, I>> {
	ensure!(authority_set.id() == authority_set_info.id, Error::<T, I>::InvalidValidatorSetId);
	ensure!(
		authority_set.len() == authority_set_info.len as usize,
		Error::<T, I>::InvalidValidatorSetLen
	);

	// Ensure that the authority set that signed the commitment is the expected one.
	let root = get_authorities_mmr_root::<T, I, _>(authority_set.validators().iter());
	ensure!(root == authority_set_info.keyset_commitment, Error::<T, I>::InvalidValidatorSetRoot);

	Ok(())
}
```

**File:** bridges/modules/beefy/src/utils.rs (L55-57)
```rust
pub(crate) fn signatures_required(validators_len: usize) -> usize {
	validators_len - validators_len.saturating_sub(1) / 3
}
```

**File:** bridges/modules/beefy/src/utils.rs (L59-93)
```rust
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
```

**File:** bridges/modules/beefy/src/utils.rs (L107-120)
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
```
